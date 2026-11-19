import base64
import json
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.billing.models import Invoice
from apps.payments.models import PaymentProvider, Transaction

pytestmark = pytest.mark.django_db

WEBHOOK = "/api/v1/payments/webhook/payme/"
KEY = "payme-cashbox-key"
AMOUNT = 2_900_000  # 29 000 so'm in tiyin


@pytest.fixture
def payme(enable_provider):
    return enable_provider("payme", merchant_id="65f0c0ffee", secret_key=KEY)


def rpc(client, method, params, key=KEY, login="Paycom"):
    token = base64.b64encode(f"{login}:{key}".encode()).decode()
    response = client.post(
        WEBHOOK,
        data=json.dumps({"jsonrpc": "2.0", "id": 7, "method": method, "params": params}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Basic {token}",
    )
    assert response.status_code == 200
    return response.json()


def create(client, invoice, payme_id="pm-1", amount=AMOUNT):
    return rpc(
        client,
        "CreateTransaction",
        {
            "id": payme_id,
            "time": 1_784_000_000_000,
            "amount": amount,
            "account": {"order_id": invoice.number},
        },
    )


def error_code(body):
    return body["error"]["code"]


def test_checkout_link_encodes_merchant_params(customer_client, invoice, payme):
    response = customer_client.post(
        "/api/v1/payments/checkout/",
        {"invoice_id": str(invoice.pk), "provider": "payme"},
        format="json",
    )
    url = response.json()["payment_url"]
    assert url.startswith("https://checkout.paycom.uz/")
    decoded = base64.b64decode(url.removeprefix("https://checkout.paycom.uz/")).decode()
    assert f"m=65f0c0ffee;ac.order_id={invoice.number};a={AMOUNT};c=" in decoded


def test_full_payment_flow(client, invoice, payme):
    body = rpc(
        client,
        "CheckPerformTransaction",
        {"amount": AMOUNT, "account": {"order_id": invoice.number}},
    )
    assert body["result"] == {"allow": True}

    created = create(client, invoice)["result"]
    assert created["state"] == 1
    # CreateTransaction is idempotent for the same Payme id.
    assert create(client, invoice)["result"] == created

    performed = rpc(client, "PerformTransaction", {"id": "pm-1"})["result"]
    assert performed["state"] == 2 and performed["perform_time"] > 0
    assert rpc(client, "PerformTransaction", {"id": "pm-1"})["result"] == performed

    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID
    assert invoice.payments.get().provider == "payme"

    checked = rpc(client, "CheckTransaction", {"id": "pm-1"})["result"]
    assert checked["state"] == 2 and checked["reason"] is None

    # Fulfilled orders cannot be cancelled.
    assert error_code(rpc(client, "CancelTransaction", {"id": "pm-1", "reason": 5})) == -31007


def test_authentication(client, invoice, payme):
    body = rpc(client, "CheckTransaction", {"id": "x"}, key="wrong")
    assert error_code(body) == -32504
    response = client.post(WEBHOOK, data="{}", content_type="application/json")
    assert error_code(response.json()) == -32504


def test_invalid_account_and_amount(client, invoice, payme):
    body = rpc(client, "CheckPerformTransaction", {"amount": AMOUNT, "account": {"order_id": "x"}})
    assert error_code(body) == -31050
    body = rpc(
        client,
        "CheckPerformTransaction",
        {"amount": 100, "account": {"order_id": invoice.number}},
    )
    assert error_code(body) == -31001


def test_second_transaction_for_same_invoice_is_rejected(client, invoice, payme):
    create(client, invoice, "pm-1")
    assert error_code(create(client, invoice, "pm-2")) == -31051


def test_cancel_created_transaction(client, invoice, payme):
    create(client, invoice)
    result = rpc(client, "CancelTransaction", {"id": "pm-1", "reason": 3})["result"]
    assert result["state"] == -1 and result["cancel_time"] > 0
    assert error_code(rpc(client, "PerformTransaction", {"id": "pm-1"})) == -31008
    assert rpc(client, "CheckTransaction", {"id": "pm-1"})["result"]["reason"] == 3


def test_expired_transaction_cannot_be_performed(client, invoice, payme):
    create(client, invoice)
    Transaction.objects.update(created_at=timezone.now() - timedelta(hours=13))
    assert error_code(rpc(client, "PerformTransaction", {"id": "pm-1"})) == -31008
    tx = Transaction.objects.get()
    assert (tx.provider_state, tx.cancel_reason) == (-1, 4)


def test_unknown_transaction_and_method(client, payme):
    assert error_code(rpc(client, "CheckTransaction", {"id": "nope"})) == -31003
    assert error_code(rpc(client, "DoSomething", {})) == -32601


def test_parse_error(client, payme):
    token = base64.b64encode(f"Paycom:{KEY}".encode()).decode()
    response = client.post(
        WEBHOOK,
        data="{not json",
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Basic {token}",
    )
    assert error_code(response.json()) == -32700


def test_get_statement(client, invoice, payme):
    create(client, invoice)
    now = int(timezone.now().timestamp() * 1000)
    result = rpc(client, "GetStatement", {"from": now - 60_000, "to": now + 60_000})["result"]
    [row] = result["transactions"]
    assert row["id"] == "pm-1" and row["amount"] == AMOUNT
    assert row["account"] == {"order_id": invoice.number}


def test_change_password(client, payme):
    assert rpc(client, "ChangePassword", {"password": "new-key"})["result"] == {"success": True}
    assert PaymentProvider.objects.get(key="payme").config["secret_key"] == "new-key"
    assert error_code(rpc(client, "CheckTransaction", {"id": "x"})) == -32504
