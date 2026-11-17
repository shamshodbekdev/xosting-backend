from decimal import Decimal

import pytest

from apps.billing.models import Invoice, Subscription

from .conftest import event_types

pytestmark = pytest.mark.django_db(transaction=True)


def _subscribe(client, plan_code="hosting-start"):
    return client.post("/api/v1/billing/subscriptions", {"plan_code": plan_code}, format="json")


def test_plans_are_public(client, plan):
    response = client.get("/api/v1/billing/plans?service_type=hosting")

    assert response.status_code == 200
    assert [p["code"] for p in response.json()] == ["hosting-start"]


def test_subscribe_creates_pending_subscription_and_invoice(customer_client, plan, fake_redis):
    response = _subscribe(customer_client)

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["subscription"]["status"] == "pending"
    assert body["invoice"]["total"] == "29000.00"
    assert body["invoice"]["number"].startswith("INV-")
    assert event_types(fake_redis) == ["invoice.created"]


def test_customers_cannot_see_each_others_invoices(customer_client, plan, staff_client):
    _subscribe(customer_client)
    invoice_id = Invoice.objects.get().id

    from rest_framework.test import APIClient

    from .conftest import make_token

    stranger = APIClient()
    stranger.credentials(
        HTTP_AUTHORIZATION=f"Bearer {make_token('00000000-0000-0000-0000-000000000001')}"
    )
    assert stranger.get(f"/api/v1/billing/invoices/{invoice_id}").status_code == 404
    assert customer_client.get(f"/api/v1/billing/invoices/{invoice_id}").status_code == 200
    assert staff_client.get(f"/api/v1/billing/invoices/{invoice_id}").status_code == 200


def test_payment_activates_subscription_and_is_idempotent(
    customer_client, staff_client, plan, fake_redis
):
    _subscribe(customer_client)
    invoice = Invoice.objects.get()
    url = f"/api/v1/billing/invoices/{invoice.id}/pay"
    payload = {"amount": "29000.00", "provider": "manual"}

    forbidden = customer_client.post(url, payload, format="json", HTTP_IDEMPOTENCY_KEY="k1")
    first = staff_client.post(url, payload, format="json", HTTP_IDEMPOTENCY_KEY="k1")
    retry = staff_client.post(url, payload, format="json", HTTP_IDEMPOTENCY_KEY="k1")

    assert forbidden.status_code == 403
    assert first.status_code == 201, first.content
    assert retry.status_code == 200
    assert retry.json()["id"] == first.json()["id"]

    subscription = Subscription.objects.get()
    assert subscription.status == Subscription.Status.ACTIVE
    assert subscription.current_period_end > subscription.current_period_start
    assert event_types(fake_redis) == ["invoice.created", "invoice.paid", "subscription.activated"]


def test_payment_rejects_wrong_amount_and_paid_invoice(customer_client, staff_client, plan):
    _subscribe(customer_client)
    invoice = Invoice.objects.get()
    url = f"/api/v1/billing/invoices/{invoice.id}/pay"

    wrong = staff_client.post(
        url, {"amount": "1.00", "provider": "manual"}, format="json", HTTP_IDEMPOTENCY_KEY="a"
    )
    ok = staff_client.post(
        url, {"amount": "29000.00", "provider": "manual"}, format="json", HTTP_IDEMPOTENCY_KEY="b"
    )
    again = staff_client.post(
        url, {"amount": "29000.00", "provider": "manual"}, format="json", HTTP_IDEMPOTENCY_KEY="c"
    )

    assert wrong.status_code == 409
    assert wrong.json()["error"]["code"] == "conflict"
    assert ok.status_code == 201
    assert again.status_code == 409


def test_cancel_pending_subscription_voids_invoice(customer_client, plan):
    subscription_id = _subscribe(customer_client).json()["subscription"]["id"]

    response = customer_client.post(f"/api/v1/billing/subscriptions/{subscription_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert Invoice.objects.get().status == Invoice.Status.VOID
    assert Decimal(Invoice.objects.get().total) == Decimal("29000.00")


def test_unauthenticated_requests_are_rejected(client, plan):
    assert client.get("/api/v1/billing/invoices").status_code == 401
