"""Click Merchant (SHOP) API: Prepare / Complete callbacks.

Click POSTs ``application/x-www-form-urlencoded`` to one URL; ``action=0`` is
Prepare and ``action=1`` is Complete. Every request is signed::

    sign_string = md5(click_trans_id + service_id + secret_key + merchant_trans_id
                      [+ merchant_prepare_id  (Complete only)]
                      + amount + action + sign_time)

Amounts are in so'm. The response is JSON with ``error`` (0 = success).
``merchant_trans_id`` is the invoice number.

config keys:
    service_id    Click service id                                 required
    merchant_id   Click merchant id (used for the payment link)    required
    secret_key    SECRET_KEY from the Click merchant cabinet       required
    merchant_user_id   optional, appended to the payment link
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, JsonResponse

from apps.billing.models import Invoice
from apps.billing.services import BillingError

from .. import services
from ..models import Transaction
from ..registry import register
from .base import BaseProvider, CheckoutSession

PAY_URL = "https://my.click.uz/services/pay"

ACTION_PREPARE = "0"
ACTION_COMPLETE = "1"

SUCCESS = 0
SIGN_CHECK_FAILED = -1
INCORRECT_AMOUNT = -2
ACTION_NOT_FOUND = -3
ALREADY_PAID = -4
ORDER_NOT_FOUND = -5
TRANSACTION_NOT_FOUND = -6
FAILED_TO_UPDATE = -7
BAD_REQUEST = -8
TRANSACTION_CANCELLED = -9

MESSAGES = {
    SUCCESS: "Success",
    SIGN_CHECK_FAILED: "SIGN CHECK FAILED!",
    INCORRECT_AMOUNT: "Incorrect parameter amount",
    ACTION_NOT_FOUND: "Action not found",
    ALREADY_PAID: "Already paid",
    ORDER_NOT_FOUND: "Order does not exist",
    TRANSACTION_NOT_FOUND: "Transaction does not exist",
    FAILED_TO_UPDATE: "Failed to update order",
    BAD_REQUEST: "Error in request from click",
    TRANSACTION_CANCELLED: "Transaction cancelled",
}

COMMON_FIELDS = (
    "click_trans_id",
    "service_id",
    "merchant_trans_id",
    "amount",
    "action",
    "error",
    "sign_time",
    "sign_string",
)


def click_signature(params: dict[str, str], secret_key: str) -> str:
    parts = [
        params["click_trans_id"],
        params["service_id"],
        secret_key,
        params["merchant_trans_id"],
    ]
    if params["action"] == ACTION_COMPLETE:
        parts.append(params["merchant_prepare_id"])
    parts += [params["amount"], params["action"], params["sign_time"]]
    # MD5 is mandated by the Click protocol.
    return hashlib.md5("".join(parts).encode(), usedforsecurity=False).hexdigest()


@register
class ClickProvider(BaseProvider):
    key = "click"
    name = "Click"
    sort_order = 20
    required_config = ("service_id", "merchant_id", "secret_key")

    def create_checkout(self, invoice: Invoice, return_url: str) -> CheckoutSession:
        query = {
            "service_id": self.setting("service_id"),
            "merchant_id": self.setting("merchant_id"),
            "amount": f"{invoice.total:.2f}",
            "transaction_param": invoice.number,
            "return_url": return_url,
        }
        if user_id := self.config.get("merchant_user_id"):
            query["merchant_user_id"] = user_id
        return CheckoutSession(payment_url=f"{PAY_URL}?{urlencode(query)}", account=invoice.number)

    # ------------------------------------------------------------ webhook ---
    def handle_webhook(self, request: HttpRequest, operation: str | None) -> HttpResponse:
        params = {key: str(value) for key, value in request.POST.items()}
        action = params.get("action")
        required = COMMON_FIELDS + (("merchant_prepare_id",) if action == ACTION_COMPLETE else ())
        if any(not params.get(name) for name in required):
            return self._reply(params, BAD_REQUEST)
        if action not in (ACTION_PREPARE, ACTION_COMPLETE):
            return self._reply(params, ACTION_NOT_FOUND)

        expected = click_signature(params, str(self.setting("secret_key")))
        if not hmac.compare_digest(expected, params["sign_string"].lower()):
            return self._reply(params, SIGN_CHECK_FAILED)
        if params["service_id"] != str(self.setting("service_id")):
            return self._reply(params, SIGN_CHECK_FAILED)
        try:
            amount = Decimal(params["amount"])
            click_error = int(params["error"])
        except (InvalidOperation, ValueError):
            return self._reply(params, BAD_REQUEST)

        if action == ACTION_PREPARE:
            return self._prepare(params, amount)
        return self._complete(params, amount, click_error)

    def _prepare(self, params: dict[str, str], amount: Decimal) -> HttpResponse:
        invoice = services.find_invoice(params["merchant_trans_id"])
        if invoice is None:
            return self._reply(params, ORDER_NOT_FOUND)
        if invoice.status == Invoice.Status.PAID:
            return self._reply(params, ALREADY_PAID)
        if not services.is_payable(invoice):
            return self._reply(params, TRANSACTION_CANCELLED)
        if amount != invoice.total:
            return self._reply(params, INCORRECT_AMOUNT)

        tx = Transaction.objects.filter(
            provider=self.record, external_id=params["click_trans_id"]
        ).first()
        if tx is None:
            tx = services.open_transaction(
                self.record, invoice, external_id=params["click_trans_id"], raw={"prepare": params}
            )
        elif tx.status == Transaction.Status.CANCELLED:
            return self._reply(params, TRANSACTION_CANCELLED)
        return self._reply(params, SUCCESS, merchant_prepare_id=tx.pk)

    def _complete(self, params: dict[str, str], amount: Decimal, click_error: int) -> HttpResponse:
        try:
            prepare_id = int(params["merchant_prepare_id"])
        except ValueError:
            return self._reply(params, TRANSACTION_NOT_FOUND)
        tx = Transaction.objects.filter(
            pk=prepare_id, provider=self.record, external_id=params["click_trans_id"]
        ).first()
        if tx is None or tx.order_id != params["merchant_trans_id"]:
            return self._reply(params, TRANSACTION_NOT_FOUND)
        if tx.status == Transaction.Status.PAID:
            return self._reply(params, ALREADY_PAID, merchant_confirm_id=tx.pk)
        if tx.status in (Transaction.Status.CANCELLED, Transaction.Status.FAILED):
            return self._reply(params, TRANSACTION_CANCELLED)
        if amount != tx.amount:
            return self._reply(params, INCORRECT_AMOUNT)

        tx.raw_response = {**tx.raw_response, "complete": params}
        tx.save(update_fields=["raw_response", "updated_at"])
        if click_error < 0:
            # Click reports the payment failed on its side.
            services.cancel_transaction(
                tx, error=f"Click error {click_error}: {params.get('error_note', '')}"
            )
            return self._reply(params, TRANSACTION_CANCELLED)
        try:
            services.complete_transaction(tx)
        except BillingError as exc:
            services.fail_transaction(tx, str(exc))
            return self._reply(params, FAILED_TO_UPDATE)
        return self._reply(params, SUCCESS, merchant_confirm_id=tx.pk)

    @staticmethod
    def _reply(params: dict[str, str], error: int, **extra) -> JsonResponse:
        body = {
            "click_trans_id": params.get("click_trans_id"),
            "merchant_trans_id": params.get("merchant_trans_id"),
            "error": error,
            "error_note": MESSAGES[error],
        }
        if params.get("action") == ACTION_COMPLETE:
            body["merchant_prepare_id"] = params.get("merchant_prepare_id")
        body.update(extra)
        return JsonResponse(body)
