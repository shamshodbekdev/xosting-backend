"""Transaction lifecycle shared by all providers.

Crediting money always goes through ``billing.services.record_payment`` with an
idempotency key derived from the provider transaction, so a webhook delivered
twice can never pay an invoice twice.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing import services as billing
from apps.billing.models import Invoice

from .models import PaymentProvider, Transaction
from .providers.base import CheckoutSession
from .registry import get_provider

logger = logging.getLogger(__name__)


class PaymentError(Exception):
    """A checkout cannot be started for this invoice/provider."""


class ProviderUnavailable(PaymentError):
    """The provider does not exist or is disabled."""


def find_invoice(account: str | None) -> Invoice | None:
    """Resolve the customer-facing account reference (the invoice number)."""
    if not account:
        return None
    return Invoice.objects.filter(number=str(account).strip()).first()


def is_payable(invoice: Invoice) -> bool:
    return invoice.status in Invoice.PAYABLE_STATUSES


def active_provider(key: str) -> PaymentProvider:
    record = PaymentProvider.objects.filter(key=key).first()
    if record is None or not record.is_active:
        raise ProviderUnavailable(f"Payment provider '{key}' is not available.")
    return record


def return_url_for(invoice: Invoice) -> str:
    base = settings.PAYMENTS_RETURN_URL
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}invoice={invoice.number}"


def start_checkout(invoice: Invoice, provider_key: str) -> CheckoutSession:
    if not is_payable(invoice):
        raise PaymentError(f"Invoice is {invoice.status} and cannot be paid.")
    if invoice.currency != "UZS":
        raise PaymentError("Online payment is only available for invoices in UZS.")
    provider = get_provider(active_provider(provider_key))
    return provider.create_checkout(invoice, return_url_for(invoice))


def open_transaction(
    record: PaymentProvider,
    invoice: Invoice,
    *,
    external_id: str = "",
    amount: Decimal | None = None,
    provider_state: int | None = None,
    provider_time: int | None = None,
    raw: dict | None = None,
) -> Transaction:
    return Transaction.objects.create(
        provider=record,
        invoice=invoice,
        order_id=invoice.number,
        external_id=external_id,
        amount=invoice.total if amount is None else amount,
        currency=invoice.currency,
        provider_state=provider_state,
        provider_time=provider_time,
        raw_response=raw or {},
    )


@transaction.atomic
def complete_transaction(tx: Transaction, *, provider_state: int | None = None) -> Transaction:
    """Credit the invoice. Idempotent; raises ``BillingError`` if it is not payable."""
    tx = Transaction.objects.select_for_update().select_related("provider").get(pk=tx.pk)
    if tx.status == Transaction.Status.PAID:
        return tx
    if tx.status != Transaction.Status.CREATED:
        raise billing.BillingError(f"Transaction is {tx.status} and cannot be completed.")

    reference = tx.external_id or str(tx.pk)
    result = billing.record_payment(
        str(tx.invoice_id),
        amount=tx.amount,
        provider=tx.provider.key,
        idempotency_key=f"{tx.provider.key}:{reference}",
        provider_transaction_id=reference,
    )
    tx.payment = result.payment
    tx.status = Transaction.Status.PAID
    tx.performed_at = timezone.now()
    if provider_state is not None:
        tx.provider_state = provider_state
    tx.save()
    logger.info(
        "payment transaction completed",
        extra={"provider": tx.provider.key, "transaction_id": tx.pk, "invoice": tx.order_id},
    )
    return tx


@transaction.atomic
def cancel_transaction(
    tx: Transaction,
    *,
    reason: int | None = None,
    provider_state: int | None = None,
    error: str = "",
) -> Transaction:
    """Cancel a transaction that has not been paid. Paid ones are left untouched."""
    tx = Transaction.objects.select_for_update().get(pk=tx.pk)
    if tx.status in (Transaction.Status.CANCELLED, Transaction.Status.PAID):
        return tx
    tx.status = Transaction.Status.CANCELLED
    tx.cancelled_at = timezone.now()
    tx.cancel_reason = reason
    if provider_state is not None:
        tx.provider_state = provider_state
    if error:
        tx.error = error[:255]
    tx.save()
    return tx


def fail_transaction(tx: Transaction, error: str) -> Transaction:
    Transaction.objects.filter(pk=tx.pk, status=Transaction.Status.CREATED).update(
        status=Transaction.Status.FAILED, error=error[:255], updated_at=timezone.now()
    )
    tx.refresh_from_db()
    logger.error(
        "payment transaction failed",
        extra={"provider_id": tx.provider_id, "transaction_id": tx.pk, "error": error},
    )
    return tx
