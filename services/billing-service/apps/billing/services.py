"""Billing domain logic.

Views and Celery tasks stay thin; every state transition lives here, runs in
a transaction and emits a domain event once the transaction commits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.events import publish_event

from .models import Invoice, InvoiceLine, InvoiceSequence, Payment, Plan, Subscription

logger = logging.getLogger(__name__)


class BillingError(Exception):
    """A business rule was violated (mapped to HTTP 409 by the API layer)."""


@dataclass(frozen=True)
class PaymentResult:
    payment: Payment
    created: bool


def _next_invoice_number(now: datetime) -> str:
    sequence, _ = InvoiceSequence.objects.select_for_update().get_or_create(year=now.year)
    sequence.last_value += 1
    sequence.save(update_fields=["last_value"])
    return f"INV-{now.year}-{sequence.last_value:06d}"


def _period_end(start: datetime, plan: Plan) -> datetime:
    return start + relativedelta(months=plan.period_months)


def _invoice_payload(invoice: Invoice) -> dict:
    return {
        "invoice_id": str(invoice.id),
        "number": invoice.number,
        "total": str(invoice.total),
        "currency": invoice.currency,
        "due_at": invoice.due_at.isoformat(),
        "subscription_id": str(invoice.subscription_id) if invoice.subscription_id else None,
    }


def _issue_invoice(
    subscription: Subscription,
    *,
    kind: str,
    now: datetime,
    period_start: datetime | None,
    due_at: datetime,
) -> Invoice:
    plan = subscription.plan
    invoice = Invoice.objects.create(
        number=_next_invoice_number(now),
        user_id=subscription.user_id,
        subscription=subscription,
        kind=kind,
        currency=plan.currency,
        total=plan.price,
        period_start=period_start,
        period_end=_period_end(period_start, plan) if period_start else None,
        issued_at=now,
        due_at=due_at,
    )
    InvoiceLine.objects.create(
        invoice=invoice,
        description=f"{plan.name} — {plan.period_months} month(s)",
        quantity=1,
        unit_price=plan.price,
        amount=plan.price,
    )
    publish_event(
        "invoice.created", user_id=str(invoice.user_id), payload=_invoice_payload(invoice)
    )
    return invoice


@transaction.atomic
def subscribe(user_id: str, plan: Plan) -> tuple[Subscription, Invoice]:
    if not plan.is_active:
        raise BillingError("This plan is no longer available.")
    now = timezone.now()
    subscription = Subscription.objects.create(user_id=user_id, plan=plan)
    invoice = _issue_invoice(
        subscription,
        kind=Invoice.Kind.NEW,
        now=now,
        period_start=None,  # the period starts when the invoice is paid
        due_at=now + timedelta(days=settings.BILLING_INVOICE_DUE_DAYS),
    )
    logger.info(
        "subscription created",
        extra={"subscription_id": str(subscription.id), "plan": plan.code},
    )
    return subscription, invoice


@transaction.atomic
def record_payment(
    invoice_id: str,
    *,
    amount: Decimal,
    provider: str,
    idempotency_key: str,
    provider_transaction_id: str = "",
    recorded_by: str | None = None,
) -> PaymentResult:
    """Apply a payment to an invoice. Safe to retry with the same idempotency key."""
    existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        if str(existing.invoice_id) != str(invoice_id):
            raise BillingError("Idempotency key was already used for another invoice.")
        return PaymentResult(existing, created=False)

    invoice = (
        Invoice.objects.select_for_update().select_related("subscription__plan").get(pk=invoice_id)
    )
    if invoice.status not in Invoice.PAYABLE_STATUSES:
        raise BillingError(f"Invoice is {invoice.status} and cannot be paid.")
    if amount != invoice.total:
        raise BillingError(f"Amount must equal the invoice total ({invoice.total}).")

    now = timezone.now()
    payment = Payment.objects.create(
        invoice=invoice,
        user_id=invoice.user_id,
        amount=amount,
        currency=invoice.currency,
        provider=provider,
        provider_transaction_id=provider_transaction_id,
        idempotency_key=idempotency_key,
        recorded_by=recorded_by,
    )
    invoice.status = Invoice.Status.PAID
    invoice.paid_at = now
    invoice.save(update_fields=["status", "paid_at", "updated_at"])
    publish_event("invoice.paid", user_id=str(invoice.user_id), payload=_invoice_payload(invoice))

    if invoice.subscription is not None:
        _apply_paid_period(invoice, now)
    return PaymentResult(payment, created=True)


def _apply_paid_period(invoice: Invoice, now: datetime) -> None:
    subscription = Subscription.objects.select_for_update().get(pk=invoice.subscription_id)
    plan = subscription.plan
    was_active = subscription.status == Subscription.Status.ACTIVE
    was_provisioned = subscription.current_period_start is not None

    start = invoice.period_start or now
    if subscription.current_period_end and subscription.current_period_end > start:
        start = subscription.current_period_end
    if not was_provisioned:
        subscription.current_period_start = start
    subscription.current_period_end = _period_end(start, plan)
    subscription.status = Subscription.Status.ACTIVE
    subscription.save()

    payload = {
        "subscription_id": str(subscription.id),
        "plan_code": plan.code,
        "service_type": plan.service_type,
        "current_period_end": subscription.current_period_end.isoformat(),
    }
    if not was_provisioned:
        event_type = "subscription.activated"
    elif not was_active:
        event_type = "subscription.reactivated"
    else:
        event_type = "subscription.renewed"
    publish_event(event_type, user_id=str(subscription.user_id), payload=payload)


@transaction.atomic
def cancel_subscription(subscription_id: str) -> Subscription:
    subscription = Subscription.objects.select_for_update().get(pk=subscription_id)
    if subscription.status == Subscription.Status.CANCELLED:
        return subscription

    now = timezone.now()
    subscription.auto_renew = False
    subscription.cancelled_at = now
    if subscription.status == Subscription.Status.PENDING:
        subscription.status = Subscription.Status.CANCELLED
    # Active subscriptions keep running until the paid period ends.
    subscription.save()
    subscription.invoices.filter(status__in=Invoice.PAYABLE_STATUSES).update(
        status=Invoice.Status.VOID, updated_at=now
    )
    publish_event(
        "subscription.cancelled",
        user_id=str(subscription.user_id),
        payload={"subscription_id": str(subscription.id), "status": subscription.status},
    )
    return subscription


def generate_renewal_invoices(now: datetime | None = None) -> int:
    """Issue renewal invoices for subscriptions whose period ends soon."""
    now = now or timezone.now()
    horizon = now + timedelta(days=settings.BILLING_RENEWAL_LEAD_DAYS)
    candidates = Subscription.objects.filter(
        status__in=[Subscription.Status.ACTIVE, Subscription.Status.PAST_DUE],
        auto_renew=True,
        current_period_end__lte=horizon,
    ).select_related("plan")

    created = 0
    for subscription in candidates.iterator():
        with transaction.atomic():
            already_issued = subscription.invoices.filter(
                kind=Invoice.Kind.RENEWAL, period_start=subscription.current_period_end
            ).exclude(status=Invoice.Status.VOID)
            if already_issued.exists():
                continue
            _issue_invoice(
                subscription,
                kind=Invoice.Kind.RENEWAL,
                now=now,
                period_start=subscription.current_period_end,
                due_at=subscription.current_period_end,
            )
            created += 1
    logger.info("renewal invoices generated", extra={"count": created})
    return created


def process_overdue_invoices(now: datetime | None = None) -> dict[str, int]:
    """Mark unpaid invoices overdue and suspend services after the grace period."""
    now = now or timezone.now()
    grace_deadline = now - timedelta(days=settings.BILLING_SUSPEND_GRACE_DAYS)
    stats = {"overdue": 0, "suspended": 0}

    with transaction.atomic():
        newly_overdue = list(
            Invoice.objects.select_for_update()
            .filter(status=Invoice.Status.OPEN, due_at__lt=now)
            .select_related("subscription")
        )
        for invoice in newly_overdue:
            invoice.status = Invoice.Status.OVERDUE
            invoice.save(update_fields=["status", "updated_at"])
            publish_event(
                "invoice.overdue", user_id=str(invoice.user_id), payload=_invoice_payload(invoice)
            )
            if invoice.subscription and invoice.subscription.status == Subscription.Status.ACTIVE:
                Subscription.objects.filter(pk=invoice.subscription_id).update(
                    status=Subscription.Status.PAST_DUE, updated_at=now
                )
        stats["overdue"] = len(newly_overdue)

    with transaction.atomic():
        to_suspend = (
            Subscription.objects.select_for_update()
            .filter(
                status=Subscription.Status.PAST_DUE,
                invoices__status=Invoice.Status.OVERDUE,
                invoices__due_at__lt=grace_deadline,
            )
            .distinct()
        )
        for subscription in to_suspend:
            subscription.status = Subscription.Status.SUSPENDED
            subscription.save(update_fields=["status", "updated_at"])
            publish_event(
                "subscription.suspended",
                user_id=str(subscription.user_id),
                payload={
                    "subscription_id": str(subscription.id),
                    "resource_ref": subscription.resource_ref,
                },
            )
            stats["suspended"] += 1

    logger.info("overdue invoices processed", extra=stats)
    return stats


def expire_cancelled_subscriptions(now: datetime | None = None) -> int:
    now = now or timezone.now()
    expired = 0
    with transaction.atomic():
        due = Subscription.objects.select_for_update().filter(
            status=Subscription.Status.ACTIVE, auto_renew=False, current_period_end__lte=now
        )
        for subscription in due:
            subscription.status = Subscription.Status.CANCELLED
            subscription.save(update_fields=["status", "updated_at"])
            publish_event(
                "subscription.expired",
                user_id=str(subscription.user_id),
                payload={
                    "subscription_id": str(subscription.id),
                    "resource_ref": subscription.resource_ref,
                },
            )
            expired += 1
    return expired
