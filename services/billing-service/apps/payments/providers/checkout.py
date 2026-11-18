"""CHECKOUT.UZ aggregator (merchant API v2).

API: ``POST https://checkout.uz/api/v2/<method>`` with ``Authorization: Bearer
<shop api key>`` and a JSON body. Amounts are in so'm.

Webhooks from CHECKOUT.UZ are **not signed**, so they are never trusted on
their own: the request must carry the shared ``webhook_token`` (embedded in
the ``webhook_url`` we register per invoice) and the payment status is then
re-read from ``status_payment`` before anything is credited. Webhooks are not
retried by CHECKOUT.UZ, so ``sync_pending`` (run by Celery beat) reconciles
transactions whose webhook was lost.

config keys:
    api_key         shop API key (Kassalar > shop > Sozlamalar)          required
    webhook_token   random secret appended to the webhook URL            required
    webhook_url     public webhook URL (default: PAYMENTS_PUBLIC_BASE_URL
                    + /api/v1/payments/webhook/checkout/)
    base_url        default https://checkout.uz/api/v2
    expire_seconds  invoice lifetime, 60..432000 (default 3600)
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse

from apps.billing.models import Invoice
from apps.billing.services import BillingError

from .. import services
from ..models import Transaction
from ..registry import register
from .base import BaseProvider, CheckoutSession, ProviderError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://checkout.uz/api/v2"
MIN_AMOUNT = Decimal("1000")
MAX_AMOUNT = Decimal("10000000")
TIMEOUT_SECONDS = 20

STATUS_PENDING = "pending"
STATUS_PAID = "paid"
STATUS_CANCELED = "canceled"
STATUS_REVERSED = "reversed"


def _json_amount(amount: Decimal) -> int | float:
    return int(amount) if amount == amount.to_integral_value() else float(amount)


@register
class CheckoutProvider(BaseProvider):
    key = "checkout"
    name = "CHECKOUT.UZ"
    sort_order = 10
    required_config = ("api_key", "webhook_token")

    # ---------------------------------------------------------------- API ---
    def _call(self, method: str, payload: dict[str, Any], **headers: str) -> dict[str, Any]:
        url = f"{str(self.setting('base_url', DEFAULT_BASE_URL)).rstrip('/')}/{method}"
        body = {k: v for k, v in payload.items() if v is not None}
        try:
            response = requests.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.setting('api_key')}",
                    "Content-Type": "application/json",
                    **headers,
                },
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"CHECKOUT.UZ {method}: {exc.__class__.__name__}") from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not response.ok or data.get("status") != "success":
            message = data.get("error") or data.get("message") or response.reason
            raise ProviderError(f"CHECKOUT.UZ {method} failed ({response.status_code}): {message}")
        return data

    def create_payment(
        self, amount: Decimal, *, description: str, return_url: str, idempotency_key: str
    ) -> dict[str, Any]:
        data = self._call(
            "create_payment",
            {
                "amount": _json_amount(amount),
                "description": description,
                "webhook_url": self.webhook_url(),
                "return_url": return_url,
                "expire_time": int(self.setting("expire_seconds", 3600)),
            },
            **{"Idempotency-Key": idempotency_key},
        )
        return data["payment"]

    def status_payment(self, payment_id: str) -> dict[str, Any]:
        return self._call("status_payment", {"id": int(payment_id)})["data"]

    def webhook_url(self) -> str:
        default = (
            f"{settings.PAYMENTS_PUBLIC_BASE_URL.rstrip('/')}/api/v1/payments/webhook/checkout/"
        )
        base = str(self.setting("webhook_url", default))
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{urlencode({'token': self.setting('webhook_token')})}"

    # ----------------------------------------------------------- checkout ---
    def create_checkout(self, invoice: Invoice, return_url: str) -> CheckoutSession:
        if not MIN_AMOUNT <= invoice.total <= MAX_AMOUNT:
            raise services.PaymentError(
                f"CHECKOUT.UZ accepts amounts between {MIN_AMOUNT} and {MAX_AMOUNT} UZS."
            )
        tx = services.open_transaction(self.record, invoice)
        try:
            payment = self.create_payment(
                invoice.total,
                description=f"Invoice {invoice.number}",
                return_url=return_url,
                idempotency_key=f"xosting-tx-{tx.pk}",
            )
        except ProviderError as exc:
            services.fail_transaction(tx, str(exc))
            raise
        tx.external_id = str(payment["_id"])
        tx.raw_response = {"create_payment": payment}
        tx.save(update_fields=["external_id", "raw_response", "updated_at"])
        return CheckoutSession(
            payment_url=payment["_url"],
            account=invoice.number,
            transaction=tx,
            extra={"uuid": payment.get("_uuid"), "pay_via": payment.get("_pay_via", {})},
        )

    # ------------------------------------------------------- reconciliation ---
    def reconcile(self, tx: Transaction) -> Transaction:
        """Apply the authoritative status from ``status_payment`` to ``tx``."""
        data = self.status_payment(tx.external_id)
        tx.raw_response = {**tx.raw_response, "status_payment": data}
        tx.save(update_fields=["raw_response", "updated_at"])
        status = data.get("status")

        if status == STATUS_PAID:
            try:
                paid_amount = Decimal(str(data.get("amount")))
            except InvalidOperation:
                paid_amount = None
            if paid_amount != tx.amount:
                return services.fail_transaction(
                    tx, f"Paid amount {data.get('amount')} does not match {tx.amount}"
                )
            try:
                return services.complete_transaction(tx)
            except BillingError as exc:
                return services.fail_transaction(tx, f"Paid but not applied: {exc}")
        if status in (STATUS_CANCELED, STATUS_REVERSED):
            if tx.status == Transaction.Status.PAID:
                logger.warning(
                    "CHECKOUT.UZ reports a paid transaction as %s; manual review required",
                    status,
                    extra={"transaction_id": tx.pk, "invoice": tx.order_id},
                )
                return tx
            return services.cancel_transaction(tx, error=f"CHECKOUT.UZ status: {status}")
        return tx

    def sync_pending(self, older_than: timedelta = timedelta(minutes=2)) -> int:
        pending = Transaction.objects.filter(
            provider=self.record, status=Transaction.Status.CREATED
        ).exclude(external_id="")
        updated = 0
        for tx in pending.iterator():
            if not tx.is_older_than(older_than):
                continue
            try:
                if self.reconcile(tx).status != Transaction.Status.CREATED:
                    updated += 1
            except ProviderError:
                logger.exception("CHECKOUT.UZ reconciliation failed", extra={"tx": tx.pk})
        return updated

    # ------------------------------------------------------------ webhook ---
    def handle_webhook(self, request: HttpRequest, operation: str | None) -> HttpResponse:
        token = request.GET.get("token", "")
        if not hmac.compare_digest(token.encode(), str(self.setting("webhook_token")).encode()):
            return JsonResponse({"status": "error", "message": "invalid token"}, status=401)
        try:
            payload = json.loads(request.body or b"{}")
            order_id = str(int(payload["data"]["order_id"]))
        except (ValueError, KeyError, TypeError):
            return JsonResponse({"status": "error", "message": "malformed payload"}, status=400)

        tx = Transaction.objects.filter(provider=self.record, external_id=order_id).first()
        if tx is None:
            return JsonResponse({"status": "error", "message": "unknown order"}, status=404)
        tx.raw_response = {**tx.raw_response, "webhook": payload}
        tx.save(update_fields=["raw_response", "updated_at"])
        try:
            tx = self.reconcile(tx)
        except ProviderError as exc:
            logger.warning("CHECKOUT.UZ status check failed: %s", exc, extra={"tx": tx.pk})
            return JsonResponse({"status": "error", "message": "status check failed"}, status=502)
        return JsonResponse({"status": "ok", "transaction": tx.status})
