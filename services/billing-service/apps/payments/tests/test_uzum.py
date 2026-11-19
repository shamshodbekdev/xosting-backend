import base64
import json

import pytest

from apps.billing.models import Invoice
from apps.payments.models import PaymentProvider, Transaction

pytestmark = pytest.mark.django_db

AMOUNT = 2_900_000


@pytest.fixture
def uzum(enable_provider):
    return enable_provider("uzum", service_id="498", login="uzum", password="pa55")


def call(client, operation, payload, auth=("uzum", "pa55")):
    token = base64.b64encode(":".join(auth).encode()).decode()
    return client.post(
        f"/api/v1/payments/webhook/uzum/{operation}/",
        data=json.dumps({"serviceId": 498, "timestamp": 1_784_000_000_000, **payload}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Basic {token}",
    )


def test_full_flow(client, invoice, uzum):
    params = {"account": invoice.number}
    response = call(client, "check", {"params": params})
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "OK"
    assert response.json()["data"]["amount"]["value"] == AMOUNT

    payload = {"transId": "uz-1", "params": params, "amount": AMOUNT}
    created = call(client, "create", payload).json()
    assert (created["status"], created["amount"]) == ("CREATED", AMOUNT)
    assert call(client, "create", payload).json()["status"] == "CREATED"  # idempotent

    confirmed = call(client, "confirm", {"transId": "uz-1"}).json()
    assert confirmed["status"] == "CONFIRMED" and confirmed["confirmTime"] > 0
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID

    status = call(client, "status", {"transId": "uz-1"}).json()
    assert status["status"] == "CONFIRMED"

    response = call(client, "reverse", {"transId": "uz-1"})
    assert response.status_code == 400
    assert response.json()["errorCode"] == 10007


def test_reverse_created_transaction(client, invoice, uzum):
    call(
        client,
        "create",
        {"transId": "uz-2", "params": {"account": invoice.number}, "amount": AMOUNT},
    )
    reversed_ = call(client, "reverse", {"transId": "uz-2"}).json()
    assert reversed_["status"] == "REVERSED"
    assert call(client, "confirm", {"transId": "uz-2"}).json()["errorCode"] == 10009
    assert Transaction.objects.get().status == Transaction.Status.CANCELLED


def test_errors(client, invoice, uzum):
    assert call(client, "check", {}, auth=("uzum", "bad")).status_code == 401
    assert call(client, "check", {"params": {"account": "nope"}}).json()["errorCode"] == 10008
    assert call(client, "dance", {"params": {}}).json()["errorCode"] == 10003
    wrong_amount = call(
        client, "create", {"transId": "uz-3", "params": {"account": invoice.number}, "amount": 1}
    )
    assert wrong_amount.json()["errorCode"] == 99999
    assert call(client, "status", {"transId": "missing"}).json()["errorCode"] == 10008


def test_service_id_must_match(client, invoice, uzum):
    PaymentProvider.objects.filter(key="uzum").update(
        config={"service_id": "1", "login": "uzum", "password": "pa55"}
    )
    response = call(client, "check", {"params": {"account": invoice.number}})
    assert response.json()["errorCode"] == 10006


def test_checkout_returns_account_without_url(customer_client, invoice, uzum):
    response = customer_client.post(
        "/api/v1/payments/checkout/",
        {"invoice_id": str(invoice.pk), "provider": "uzum"},
        format="json",
    )
    body = response.json()
    assert response.status_code == 201
    assert body["payment_url"] is None and body["account"] == invoice.number
