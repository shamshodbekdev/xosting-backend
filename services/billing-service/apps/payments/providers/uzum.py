"""Uzum Bank Merchant API (payments from the Uzum Bank app).

Uzum's processing centre calls five endpoints on our side:
``/api/v1/payments/webhook/uzum/<check|create|confirm|reverse|status>/``.
Requests are JSON, authenticated with HTTP Basic (login/password issued by
Uzum) and carry our ``serviceId``. Amounts are in tiyin, timestamps are epoch
milliseconds. ``params[<account_field>]`` holds the invoice number.

Errors are answered with HTTP 400 (401 for authentication) and
``{"status": "FAILED", "errorCode": ...}``. Confirmed payments are never
reversed here because the service has already been provisioned; such
requests are rejected with ``10007`` and refunded by staff.

config keys:
    service_id      serviceId assigned by Uzum                     required
    login           Basic auth login                               required
    password        Basic auth password                            required
    account_field   name of the payment parameter (default "account")
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone

from apps.billing.models import Invoice
from apps.billing.services import BillingError

from .. import services
from ..models import Transaction, epoch_ms, to_tiyin
from ..registry import register
from .base import BaseProvider, CheckoutSession

STATUS_OK = "OK"
STATUS_FAILED = "FAILED"
STATUS_CREATED = "CREATED"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_REVERSED = "REVERSED"

ERR_AUTH = 10001
ERR_JSON = 10002
ERR_OPERATION = 10003
ERR_MISSING_PARAMS = 10005
ERR_SERVICE_ID = 10006
ERR_ALREADY_PROCESSED = 10007
ERR_NOT_FOUND = 10008
ERR_CANCELLED = 10009
ERR_PAYMENT_DATA = 99999

OPERATIONS = ("check", "create", "confirm", "reverse", "status")


class UzumError(Exception):
    def __init__(self, code: int, http_status: int = 400, **extra: Any):
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.extra = extra


@register
class UzumProvider(BaseProvider):
    key = "uzum"
    name = "Uzum Bank"
    sort_order = 50
    required_config = ("service_id", "login", "password")

    @property
    def account_field(self) -> str:
        return str(self.setting("account_field", "account"))

    def create_checkout(self, invoice: Invoice, return_url: str) -> CheckoutSession:
        # Payment happens inside the Uzum Bank app: the customer picks our
        # service and enters the invoice number.
        return CheckoutSession(
            payment_url=None, account=invoice.number, extra={"field": self.account_field}
        )

    # ------------------------------------------------------------ webhook ---
    def handle_webhook(self, request: HttpRequest, operation: str | None) -> HttpResponse:
        service_id = None
        try:
            self._authenticate(request)
            try:
                payload = json.loads(request.body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise UzumError(ERR_JSON) from exc
            if not isinstance(payload, dict):
                raise UzumError(ERR_JSON)
            service_id = payload.get("serviceId")
            if str(service_id) != str(self.setting("service_id")):
                raise UzumError(ERR_SERVICE_ID)
            if operation not in OPERATIONS:
                raise UzumError(ERR_OPERATION)
            result = getattr(self, f"op_{operation}")(payload)
        except UzumError as exc:
            body = {
                "serviceId": service_id,
                "timestamp": epoch_ms(timezone.now()),
                "status": STATUS_FAILED,
                "errorCode": exc.code,
                **exc.extra,
            }
            return JsonResponse(body, status=exc.http_status)
        return JsonResponse(
            {"serviceId": service_id, "timestamp": epoch_ms(timezone.now()), **result}
        )

    def _authenticate(self, request: HttpRequest) -> None:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "basic" or not token:
            raise UzumError(ERR_AUTH, 401)
        try:
            login, _, password = base64.b64decode(token, validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise UzumError(ERR_AUTH, 401) from exc
        valid_login = hmac.compare_digest(login.encode(), str(self.setting("login")).encode())
        valid_pass = hmac.compare_digest(password.encode(), str(self.setting("password")).encode())
        if not (valid_login and valid_pass):
            raise UzumError(ERR_AUTH, 401)

    # ------------------------------------------------------------ helpers ---
    def _invoice(self, payload: dict[str, Any]) -> Invoice:
        params = payload.get("params")
        if not isinstance(params, dict) or not params.get(self.account_field):
            raise UzumError(ERR_MISSING_PARAMS)
        invoice = services.find_invoice(params[self.account_field])
        if invoice is None:
            raise UzumError(ERR_NOT_FOUND)
        if not services.is_payable(invoice):
            raise UzumError(ERR_ALREADY_PROCESSED)
        return invoice

    def _transaction(self, payload: dict[str, Any]) -> Transaction:
        trans_id = payload.get("transId")
        if not trans_id:
            raise UzumError(ERR_MISSING_PARAMS)
        tx = Transaction.objects.filter(provider=self.record, external_id=str(trans_id)).first()
        if tx is None:
            raise UzumError(ERR_NOT_FOUND, transId=trans_id)
        return tx

    def _account_data(self, order_id: str) -> dict[str, Any]:
        return {"account": {"value": order_id}}

    # --------------------------------------------------------- operations ---
    def op_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        invoice = self._invoice(payload)
        return {
            "status": STATUS_OK,
            "data": {
                **self._account_data(invoice.number),
                "amount": {"value": to_tiyin(invoice.total)},
            },
        }

    def op_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        trans_id = payload.get("transId")
        if not trans_id:
            raise UzumError(ERR_MISSING_PARAMS)
        tx = Transaction.objects.filter(provider=self.record, external_id=str(trans_id)).first()
        if tx is None:
            invoice = self._invoice(payload)
            amount = payload.get("amount")
            if not isinstance(amount, int | float) or int(amount) != to_tiyin(invoice.total):
                raise UzumError(ERR_PAYMENT_DATA, transId=trans_id)
            tx = services.open_transaction(
                self.record,
                invoice,
                external_id=str(trans_id),
                provider_time=ts if isinstance(ts := payload.get("timestamp"), int) else None,
                raw={"create": payload},
            )
        elif tx.status != Transaction.Status.CREATED:
            raise UzumError(ERR_ALREADY_PROCESSED, transId=trans_id)
        return {
            "transId": tx.external_id,
            "status": STATUS_CREATED,
            "transTime": epoch_ms(tx.created_at),
            "data": self._account_data(tx.order_id),
            "amount": tx.amount_tiyin,
        }

    def op_confirm(self, payload: dict[str, Any]) -> dict[str, Any]:
        tx = self._transaction(payload)
        if tx.status in (Transaction.Status.CANCELLED, Transaction.Status.FAILED):
            raise UzumError(ERR_CANCELLED, transId=tx.external_id)
        if tx.status == Transaction.Status.CREATED:
            tx.raw_response = {**tx.raw_response, "confirm": payload}
            tx.save(update_fields=["raw_response", "updated_at"])
            try:
                tx = services.complete_transaction(tx)
            except BillingError as exc:
                services.fail_transaction(tx, str(exc))
                raise UzumError(ERR_PAYMENT_DATA, transId=tx.external_id) from exc
        return {
            "transId": tx.external_id,
            "status": STATUS_CONFIRMED,
            "confirmTime": epoch_ms(tx.performed_at),
            "data": self._account_data(tx.order_id),
            "amount": tx.amount_tiyin,
        }

    def op_reverse(self, payload: dict[str, Any]) -> dict[str, Any]:
        tx = self._transaction(payload)
        if tx.status == Transaction.Status.PAID:
            raise UzumError(ERR_ALREADY_PROCESSED, transId=tx.external_id)
        tx = services.cancel_transaction(tx, error="reversed by Uzum")
        return {
            "transId": tx.external_id,
            "status": STATUS_REVERSED,
            "reverseTime": epoch_ms(tx.cancelled_at),
            "data": self._account_data(tx.order_id),
            "amount": tx.amount_tiyin,
        }

    def op_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        tx = self._transaction(payload)
        status = {
            Transaction.Status.CREATED: STATUS_CREATED,
            Transaction.Status.PAID: STATUS_CONFIRMED,
            Transaction.Status.CANCELLED: STATUS_REVERSED,
        }.get(tx.status, STATUS_FAILED)
        return {
            "transId": tx.external_id,
            "status": status,
            "transTime": epoch_ms(tx.created_at),
            "confirmTime": epoch_ms(tx.performed_at) or None,
            "reverseTime": epoch_ms(tx.cancelled_at) or None,
            "data": self._account_data(tx.order_id),
            "amount": tx.amount_tiyin,
        }
