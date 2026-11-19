from urllib.parse import parse_qs, urlparse

import pytest

from apps.billing.models import Invoice
from apps.payments.models import PaymentProvider, Transaction
from apps.payments.providers.click import click_signature

pytestmark = pytest.mark.django_db

WEBHOOK = "/api/v1/payments/webhook/click/"
SECRET = "click-secret"


@pytest.fixture
def click(enable_provider):
    return enable_provider("click", service_id="101", merchant_id="202", secret_key=SECRET)


def signed(**params) -> dict[str, str]:
    params = {k: str(v) for k, v in params.items()}
    params["sign_string"] = click_signature(params, SECRET)
    return params


def prepare(client, invoice, click_trans_id="9001", amount=None, **overrides):
    params = signed(
        click_trans_id=click_trans_id,
        service_id="101",
        click_paydoc_id="555",
        merchant_trans_id=invoice.number,
        amount=amount if amount is not None else invoice.total,
        action=0,
        error=0,
        error_note="Success",
        sign_time="2026-11-19 10:00:00",
    )
    params.update(overrides)
    return client.post(WEBHOOK, params)


def complete(client, invoice, prepare_id, click_trans_id="9001", error=0, amount=None):
    params = signed(
        click_trans_id=click_trans_id,
        service_id="101",
        click_paydoc_id="555",
        merchant_trans_id=invoice.number,
        merchant_prepare_id=prepare_id,
        amount=amount if amount is not None else invoice.total,
        action=1,
        error=error,
        error_note="Success",
        sign_time="2026-11-19 10:00:05",
    )
    return client.post(WEBHOOK, params)


def test_payment_link(customer_client, invoice, click):
    response = customer_client.post(
        "/api/v1/payments/checkout/",
        {"invoice_id": str(invoice.pk), "provider": "click"},
        format="json",
    )
    assert response.status_code == 201
    url = urlparse(response.json()["payment_url"])
    query = parse_qs(url.query)
    assert url.netloc == "my.click.uz"
    assert query["service_id"] == ["101"]
    assert query["transaction_param"] == [invoice.number]
    assert query["amount"] == ["29000.00"]


def test_prepare_then_complete_pays_invoice(client, invoice, click):
    body = prepare(client, invoice).json()
    assert body["error"] == 0, body
    prepare_id = body["merchant_prepare_id"]

    body = complete(client, invoice, prepare_id).json()
    assert body["error"] == 0, body
    assert body["merchant_confirm_id"] == prepare_id
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID

    # Repeated Complete is reported as already paid, nothing is charged twice.
    assert complete(client, invoice, prepare_id).json()["error"] == -4
    assert invoice.payments.count() == 1


def test_bad_signature(client, invoice, click):
    assert prepare(client, invoice, sign_string="0" * 32).json()["error"] == -1


def test_unknown_order(client, invoice, click):
    params = signed(
        click_trans_id="1",
        service_id="101",
        click_paydoc_id="1",
        merchant_trans_id="INV-0000-000000",
        amount="29000",
        action=0,
        error=0,
        sign_time="2026-11-19 10:00:00",
    )
    assert client.post(WEBHOOK, params).json()["error"] == -5


def test_wrong_amount(client, invoice, click):
    assert prepare(client, invoice, amount="1000").json()["error"] == -2


def test_already_paid_invoice(client, invoice, click):
    prepare_id = prepare(client, invoice).json()["merchant_prepare_id"]
    complete(client, invoice, prepare_id)
    assert prepare(client, invoice, click_trans_id="9002").json()["error"] == -4


def test_complete_with_click_error_cancels(client, invoice, click):
    prepare_id = prepare(client, invoice).json()["merchant_prepare_id"]
    assert complete(client, invoice, prepare_id, error=-5017).json()["error"] == -9
    assert Transaction.objects.get().status == Transaction.Status.CANCELLED
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.OPEN


def test_complete_unknown_prepare_id(client, invoice, click):
    assert complete(client, invoice, 424242).json()["error"] == -6


def test_missing_parameters(client, click):
    assert client.post(WEBHOOK, {"action": "0"}).json()["error"] == -8


def test_disabled_provider_is_403(client, invoice, click):
    PaymentProvider.objects.filter(key="click").update(is_active=False)
    assert prepare(client, invoice).status_code == 403
