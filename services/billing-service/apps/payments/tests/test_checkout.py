import json

import pytest
import requests

from apps.billing.models import Invoice, Subscription
from apps.payments.models import PaymentProvider, Transaction
from apps.payments.registry import get_provider

pytestmark = pytest.mark.django_db

WEBHOOK = "/api/v1/payments/webhook/checkout/"
CONFIG = {
    "api_key": "shop-key",
    "webhook_token": "s3cret-token",
    "webhook_url": "https://api.xosting.test/api/v1/payments/webhook/checkout/",
}


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.ok = status_code < 400
        self.reason = "ERR"

    def json(self):
        return self._data


class FakeCheckoutApi:
    """Records calls and answers like the CHECKOUT.UZ v2 API."""

    def __init__(self):
        self.calls = []
        self.status = "pending"
        self.amount = 29000

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        method = url.rsplit("/", 1)[-1]
        if method == "create_payment":
            return FakeResponse(
                {
                    "status": "success",
                    "payment": {
                        "_id": 4518,
                        "_uuid": "78144b2f-0000-4000-8000-000000000000",
                        "_url": "https://checkout.uz/pay/78144b2f",
                        "_amount": json["amount"],
                        "_status": "pending",
                        "_pay_via": {"click": "https://checkout.uz/pay/78144b2f/click"},
                    },
                }
            )
        if method == "status_payment":
            return FakeResponse(
                {
                    "status": "success",
                    "data": {"id": json["id"], "amount": self.amount, "status": self.status},
                }
            )
        return FakeResponse({"status": "error", "error": "unknown"}, 404)


@pytest.fixture
def api(monkeypatch):
    fake = FakeCheckoutApi()
    monkeypatch.setattr("apps.payments.providers.checkout.requests.post", fake)
    return fake


@pytest.fixture
def checkout(enable_provider):
    return enable_provider("checkout", **CONFIG)


def start(client, invoice, provider="checkout"):
    return client.post(
        "/api/v1/payments/checkout/",
        {"invoice_id": str(invoice.pk), "provider": provider},
        format="json",
    )


def notify(client, order_id=4518, token="s3cret-token"):
    payload = {
        "webhook_type": "version_1_1",
        "status": "success",
        "event": "payment_confirmed",
        "payment_system": "click",
        "data": {"order_id": order_id, "amount": 29000, "currency": "UZS", "status": "paid"},
    }
    return client.post(
        f"{WEBHOOK}?token={token}", data=json.dumps(payload), content_type="application/json"
    )


def test_providers_are_seeded_disabled_and_listing_shows_only_active(client, checkout):
    assert PaymentProvider.objects.filter(key="checkout").exists()
    response = client.get("/api/v1/payments/providers/")
    assert response.status_code == 200
    assert [p["key"] for p in response.json()] == ["checkout"]


def test_checkout_creates_payment_and_returns_url(customer_client, invoice, checkout, api):
    response = start(customer_client, invoice)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["payment_url"] == "https://checkout.uz/pay/78144b2f"
    assert body["account"] == invoice.number

    call = api.calls[0]
    assert call["url"] == "https://checkout.uz/api/v2/create_payment"
    assert call["headers"]["Authorization"] == "Bearer shop-key"
    assert call["headers"]["Idempotency-Key"].startswith("xosting-tx-")
    assert call["json"]["amount"] == 29000
    assert call["json"]["webhook_url"].endswith("?token=s3cret-token")
    tx = Transaction.objects.get()
    assert (tx.external_id, tx.status) == ("4518", Transaction.Status.CREATED)


def test_checkout_rejects_foreign_invoice(customer_client, other_invoice, checkout, api):
    assert start(customer_client, other_invoice).status_code == 404
    assert api.calls == []


def test_checkout_with_disabled_provider_is_404(customer_client, invoice, api):
    assert start(customer_client, invoice).status_code == 404


def test_checkout_api_failure_is_502_and_marks_transaction_failed(
    customer_client, invoice, checkout, monkeypatch
):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr("apps.payments.providers.checkout.requests.post", boom)
    assert start(customer_client, invoice).status_code == 502
    assert Transaction.objects.get().status == Transaction.Status.FAILED


def test_webhook_pays_invoice_after_status_check(client, customer_client, invoice, checkout, api):
    start(customer_client, invoice)
    api.status = "paid"
    response = notify(client)
    assert response.status_code == 200, response.content
    assert api.calls[-1]["json"] == {"id": 4518}

    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID
    assert invoice.subscription.status == Subscription.Status.ACTIVE
    payment = invoice.payments.get()
    assert (payment.provider, payment.provider_transaction_id) == ("checkout", "4518")

    # A duplicate delivery is harmless.
    assert notify(client).status_code == 200
    assert invoice.payments.count() == 1


def test_webhook_does_not_trust_payload_when_status_is_pending(
    client, customer_client, invoice, checkout, api
):
    start(customer_client, invoice)
    assert notify(client).status_code == 200
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.OPEN


def test_webhook_amount_mismatch_fails_transaction(client, customer_client, invoice, checkout, api):
    start(customer_client, invoice)
    api.status, api.amount = "paid", 1000
    notify(client)
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.OPEN
    assert Transaction.objects.get().status == Transaction.Status.FAILED


def test_webhook_requires_token(client, customer_client, invoice, checkout, api):
    start(customer_client, invoice)
    assert notify(client, token="wrong").status_code == 401


def test_webhook_unknown_order(client, checkout, api):
    assert notify(client, order_id=999).status_code == 404


def test_webhook_for_disabled_provider_is_403(client, checkout):
    PaymentProvider.objects.filter(key="checkout").update(is_active=False)
    assert notify(client).status_code == 403


def test_webhook_for_misconfigured_provider_is_503(client, enable_provider):
    enable_provider("checkout")
    assert notify(client).status_code == 503


def test_cancelled_status_cancels_transaction(client, customer_client, invoice, checkout, api):
    start(customer_client, invoice)
    api.status = "canceled"
    notify(client)
    assert Transaction.objects.get().status == Transaction.Status.CANCELLED


def test_sync_pending_reconciles_lost_webhooks(customer_client, invoice, checkout, api):
    start(customer_client, invoice)
    Transaction.objects.update(created_at="2020-01-01T00:00:00Z")
    api.status = "paid"
    assert get_provider(checkout).sync_pending() == 1
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID
