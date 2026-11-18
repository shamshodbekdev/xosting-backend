"""Celery tasks for payment providers."""

from celery import shared_task

from .models import PaymentProvider
from .registry import get_provider


@shared_task(name="payments.sync_checkout_transactions")
def sync_checkout_transactions() -> int:
    """Reconcile CHECKOUT.UZ transactions whose webhook never arrived."""
    record = PaymentProvider.objects.filter(key="checkout", is_active=True).first()
    if record is None:
        return 0
    provider = get_provider(record)
    if provider.missing_config():
        return 0
    return provider.sync_pending()
