import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.payments.models import PaymentProvider

pytestmark = pytest.mark.django_db

ADMIN = "/api/v1/billing/admin/"
CHANGELIST = f"{ADMIN}payments/paymentprovider/"


@pytest.fixture
def admin_client():
    user = get_user_model().objects.create_superuser("root", "root@example.com", "pw")
    client = Client()
    client.force_login(user)
    return client


def changelist_post(client, data: dict, **extra):
    providers = list(PaymentProvider.objects.order_by("sort_order", "name"))
    form = {
        "form-TOTAL_FORMS": str(len(providers)),
        "form-INITIAL_FORMS": str(len(providers)),
        "_save": "Save",
    }
    for index, provider in enumerate(providers):
        form[f"form-{index}-id"] = str(provider.pk)
        form[f"form-{index}-sort_order"] = str(provider.sort_order)
        if data.get(provider.key, provider.is_active):
            form[f"form-{index}-is_active"] = "on"
    return client.post(CHANGELIST, form, **extra)


def test_dashboard_has_payment_providers_section(admin_client):
    response = admin_client.get(ADMIN)
    assert response.status_code == 200
    assert b"Payment Providers" in response.content


def test_changelist_lists_every_provider(admin_client):
    response = admin_client.get(CHANGELIST)
    assert response.status_code == 200
    for name in (b"CHECKOUT.UZ", b"Click", b"Payme", b"Paynet", b"Uzum Bank"):
        assert name in response.content


def test_toggle_on_and_off_from_changelist(admin_client):
    PaymentProvider.objects.filter(key="click").update(
        config={"service_id": "1", "merchant_id": "2", "secret_key": "s"}
    )
    response = changelist_post(admin_client, {"click": True})
    assert response.status_code == 302
    assert PaymentProvider.objects.get(key="click").is_active

    changelist_post(admin_client, {"click": False})
    assert not PaymentProvider.objects.get(key="click").is_active


def test_cannot_enable_unconfigured_provider(admin_client):
    response = changelist_post(admin_client, {"payme": True})
    assert response.status_code == 200  # form redisplayed with an error
    assert b"missing config keys" in response.content
    assert not PaymentProvider.objects.get(key="payme").is_active


def test_bulk_actions(admin_client):
    PaymentProvider.objects.filter(key="uzum").update(
        config={"service_id": "1", "login": "l", "password": "p"}
    )
    ids = [str(p.pk) for p in PaymentProvider.objects.filter(key__in=["uzum", "paynet"])]
    admin_client.post(
        CHANGELIST, {"action": "enable_providers", "_selected_action": ids}, follow=True
    )
    assert PaymentProvider.objects.get(key="uzum").is_active
    assert not PaymentProvider.objects.get(key="paynet").is_active  # not configured

    admin_client.post(
        CHANGELIST, {"action": "disable_providers", "_selected_action": ids}, follow=True
    )
    assert not PaymentProvider.objects.get(key="uzum").is_active


def test_change_form_shows_webhook_url(admin_client):
    provider = PaymentProvider.objects.get(key="payme")
    response = admin_client.get(f"{CHANGELIST}{provider.pk}/change/")
    assert response.status_code == 200
    assert b"/api/v1/payments/webhook/payme/" in response.content


def test_transactions_are_read_only(admin_client):
    response = admin_client.get(f"{ADMIN}payments/transaction/")
    assert response.status_code == 200
    assert admin_client.get(f"{ADMIN}payments/transaction/add/").status_code == 403
