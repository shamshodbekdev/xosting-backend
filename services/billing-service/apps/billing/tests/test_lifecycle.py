from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.billing import services
from apps.billing.models import Invoice, Subscription

from .conftest import event_types

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def active_subscription(plan, customer_id):
    subscription, invoice = services.subscribe(customer_id, plan)
    services.record_payment(
        str(invoice.id), amount=Decimal("29000.00"), provider="manual", idempotency_key="init"
    )
    subscription.refresh_from_db()
    return subscription


def test_renewal_invoice_generated_once_per_period(active_subscription):
    now = active_subscription.current_period_end - timedelta(days=2)

    assert services.generate_renewal_invoices(now) == 1
    assert services.generate_renewal_invoices(now) == 0  # idempotent

    renewal = Invoice.objects.get(kind=Invoice.Kind.RENEWAL)
    assert renewal.period_start == active_subscription.current_period_end
    assert renewal.due_at == active_subscription.current_period_end


def test_paying_renewal_extends_period(active_subscription):
    old_end = active_subscription.current_period_end
    services.generate_renewal_invoices(old_end - timedelta(days=1))
    renewal = Invoice.objects.get(kind=Invoice.Kind.RENEWAL)

    services.record_payment(
        str(renewal.id),
        amount=renewal.total,
        provider="payme",
        idempotency_key="renew-1",
        provider_transaction_id="payme-123",
    )

    active_subscription.refresh_from_db()
    assert active_subscription.current_period_end > old_end
    assert active_subscription.status == Subscription.Status.ACTIVE


def test_unpaid_renewal_goes_overdue_then_suspends(active_subscription, fake_redis, settings):
    end = active_subscription.current_period_end
    services.generate_renewal_invoices(end - timedelta(days=1))

    stats = services.process_overdue_invoices(end + timedelta(hours=1))
    active_subscription.refresh_from_db()
    assert stats == {"overdue": 1, "suspended": 0}
    assert active_subscription.status == Subscription.Status.PAST_DUE

    later = end + timedelta(days=settings.BILLING_SUSPEND_GRACE_DAYS + 1)
    stats = services.process_overdue_invoices(later)
    active_subscription.refresh_from_db()
    assert stats == {"overdue": 0, "suspended": 1}
    assert active_subscription.status == Subscription.Status.SUSPENDED
    assert "subscription.suspended" in event_types(fake_redis)


def test_cancelled_active_subscription_expires_at_period_end(active_subscription):
    services.cancel_subscription(str(active_subscription.id))
    active_subscription.refresh_from_db()
    assert active_subscription.status == Subscription.Status.ACTIVE  # paid period honoured

    assert services.expire_cancelled_subscriptions(timezone.now()) == 0
    expired = services.expire_cancelled_subscriptions(
        active_subscription.current_period_end + timedelta(seconds=1)
    )
    active_subscription.refresh_from_db()
    assert expired == 1
    assert active_subscription.status == Subscription.Status.CANCELLED


def test_invoice_numbers_are_sequential(plan, customer_id):
    _, first = services.subscribe(customer_id, plan)
    _, second = services.subscribe(customer_id, plan)

    first_seq = int(first.number.rsplit("-", 1)[1])
    assert int(second.number.rsplit("-", 1)[1]) == first_seq + 1
