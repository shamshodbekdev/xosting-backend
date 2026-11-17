"""Celery tasks: thin, idempotent wrappers around the service layer."""

from celery import shared_task

from . import services


@shared_task(
    name="billing.generate_renewal_invoices",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
)
def generate_renewal_invoices() -> int:
    return services.generate_renewal_invoices()


@shared_task(
    name="billing.process_overdue_invoices",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
)
def process_overdue_invoices() -> dict[str, int]:
    return services.process_overdue_invoices()


@shared_task(
    name="billing.expire_cancelled_subscriptions",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
)
def expire_cancelled_subscriptions() -> int:
    return services.expire_cancelled_subscriptions()
