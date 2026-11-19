"""Payme (Paycom) Merchant API — JSON-RPC 2.0.

Payme calls our endpoint with ``Authorization: Basic base64("Paycom:<key>")``
and a JSON-RPC body. Responses are always HTTP 200; errors go in ``error``.
Amounts are in tiyin; times are epoch milliseconds. ``account[<field>]`` holds
the invoice number.

Transaction states: 1 created, 2 performed, -1 cancelled before perform,
-2 cancelled after perform. A created transaction expires after 12 hours.
Performed transactions are never cancelled here (the invoice has already been
fulfilled), so CancelTransaction answers -31007 for them; refunds are handled
by staff.

config keys:
    merchant_id     Payme merchant (cashbox) id                    required
    secret_key      cashbox key (test key in the sandbox)          required
    account_field   account parameter name (default "order_id")
    login           Basic auth login (default "Paycom")
    test_mode       true -> checkout links point to test.paycom.uz
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse

from apps.billing.models import Invoice
from apps.billing.services import BillingError

from .. import services
from ..models import Transaction, epoch_ms, to_tiyin
from ..registry import register
from .base import BaseProvider, CheckoutSession

logger = logging.getLogger(__name__)

CHECKOUT_URL = "https://checkout.paycom.uz"
TEST_CHECKOUT_URL = "https://test.paycom.uz"
TRANSACTION_TIMEOUT = timedelta(hours=12)

STATE_CREATED = 1
STATE_PERFORMED = 2
STATE_CANCELLED = -1
STATE_CANCELLED_AFTER_PERFORM = -2
REASON_TIMEOUT = 4

ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INSUFFICIENT_PRIVILEGE = -32504
ERR_INTERNAL = -32400
ERR_INVALID_AMOUNT = -31001
ERR_TRANSACTION_NOT_FOUND = -31003
ERR_CANNOT_CANCEL = -31007
ERR_CANNOT_PERFORM = -31008
ERR_INVALID_ACCOUNT = -31050
ERR_ORDER_BUSY = -31051

MESSAGES: dict[int, dict[str, str]] = {
    ERR_PARSE: {"ru": "Ошибка разбора JSON", "uz": "JSON xatosi", "en": "Parse error"},
    ERR_INVALID_REQUEST: {
        "ru": "Неверный JSON-RPC объект",
        "uz": "JSON-RPC obyekti noto'g'ri",
        "en": "Invalid JSON-RPC object",
    },
    ERR_METHOD_NOT_FOUND: {
        "ru": "Метод не найден",
        "uz": "Metod topilmadi",
        "en": "Method not found",
    },
    ERR_INVALID_PARAMS: {
        "ru": "Неверные параметры",
        "uz": "Parametrlar noto'g'ri",
        "en": "Invalid params",
    },
    ERR_INSUFFICIENT_PRIVILEGE: {
        "ru": "Недостаточно привилегий",
        "uz": "Ruxsat yetarli emas",
        "en": "Insufficient privilege",
    },
    ERR_INTERNAL: {"ru": "Системная ошибка", "uz": "Tizim xatosi", "en": "Internal error"},
    ERR_INVALID_AMOUNT: {
        "ru": "Неверная сумма",
        "uz": "Noto'g'ri summa",
        "en": "Invalid amount",
    },
    ERR_TRANSACTION_NOT_FOUND: {
        "ru": "Транзакция не найдена",
        "uz": "Tranzaksiya topilmadi",
        "en": "Transaction not found",
    },
    ERR_CANNOT_CANCEL: {
        "ru": "Заказ выполнен. Невозможно отменить транзакцию",
        "uz": "Buyurtma bajarilgan. Tranzaksiyani bekor qilib bo'lmaydi",
        "en": "Order is fulfilled. The transaction cannot be cancelled",
    },
    ERR_CANNOT_PERFORM: {
        "ru": "Невозможно выполнить операцию",
        "uz": "Operatsiyani bajarib bo'lmaydi",
        "en": "Unable to perform the operation",
    },
    ERR_INVALID_ACCOUNT: {
        "ru": "Счёт не найден или не может быть оплачен",
        "uz": "Hisob topilmadi yoki to'lab bo'lmaydi",
        "en": "Invoice not found or cannot be paid",
    },
    ERR_ORDER_BUSY: {
        "ru": "Счёт уже ожидает оплату в другой транзакции",
        "uz": "Hisob boshqa tranzaksiyada to'lanmoqda",
        "en": "Invoice is being paid by another transaction",
    },
}


class RpcError(Exception):
    def __init__(self, code: int, data: str | None = None):
        super().__init__(code)
        self.code = code
        self.data = data


@register
class PaymeProvider(BaseProvider):
    key = "payme"
    name = "Payme"
    sort_order = 30
    required_config = ("merchant_id", "secret_key")

    @property
    def account_field(self) -> str:
        return str(self.setting("account_field", "order_id"))

    def create_checkout(self, invoice: Invoice, return_url: str) -> CheckoutSession:
        params = (
            f"m={self.setting('merchant_id')};ac.{self.account_field}={invoice.number};"
            f"a={to_tiyin(invoice.total)};c={return_url}"
        )
        base = TEST_CHECKOUT_URL if self.setting("test_mode", False) else CHECKOUT_URL
        encoded = base64.b64encode(params.encode()).decode()
        return CheckoutSession(payment_url=f"{base}/{encoded}", account=invoice.number)

    # ------------------------------------------------------------ webhook ---
    def handle_webhook(self, request: HttpRequest, operation: str | None) -> HttpResponse:
        request_id: Any = None
        try:
            self._authenticate(request)
            try:
                payload = json.loads(request.body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise RpcError(ERR_PARSE) from exc
            if not isinstance(payload, dict):
                raise RpcError(ERR_INVALID_REQUEST)
            request_id = payload.get("id")
            method = payload.get("method")
            params = payload.get("params")
            if not isinstance(method, str) or not isinstance(params, dict):
                raise RpcError(ERR_INVALID_REQUEST)
            handler = self._methods().get(method)
            if handler is None:
                raise RpcError(ERR_METHOD_NOT_FOUND, method)
            result = handler(params)
        except RpcError as exc:
            return self._error(request_id, exc.code, exc.data)
        except (KeyError, TypeError, ValueError) as exc:
            return self._error(request_id, ERR_INVALID_PARAMS, str(exc))
        return JsonResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _methods(self):
        return {
            "CheckPerformTransaction": self.check_perform_transaction,
            "CreateTransaction": self.create_transaction,
            "PerformTransaction": self.perform_transaction,
            "CancelTransaction": self.cancel_transaction,
            "CheckTransaction": self.check_transaction,
            "GetStatement": self.get_statement,
            "ChangePassword": self.change_password,
        }

    def _authenticate(self, request: HttpRequest) -> None:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "basic" or not token:
            raise RpcError(ERR_INSUFFICIENT_PRIVILEGE)
        try:
            login, _, password = base64.b64decode(token, validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise RpcError(ERR_INSUFFICIENT_PRIVILEGE) from exc
        expected_login = str(self.setting("login", "Paycom"))
        valid_login = hmac.compare_digest(login.encode(), expected_login.encode())
        valid_key = hmac.compare_digest(password.encode(), str(self.setting("secret_key")).encode())
        if not (valid_login and valid_key):
            raise RpcError(ERR_INSUFFICIENT_PRIVILEGE)

    @staticmethod
    def _error(request_id: Any, code: int, data: str | None) -> JsonResponse:
        error: dict[str, Any] = {
            "code": code,
            "message": MESSAGES.get(code, MESSAGES[ERR_INTERNAL]),
        }
        if data is not None:
            error["data"] = data
        return JsonResponse({"jsonrpc": "2.0", "id": request_id, "error": error})

    # ------------------------------------------------------------ helpers ---
    def _payable_invoice(self, params: dict[str, Any]) -> Invoice:
        account = params.get("account")
        if not isinstance(account, dict):
            raise RpcError(ERR_INVALID_ACCOUNT, self.account_field)
        invoice = services.find_invoice(account.get(self.account_field))
        if invoice is None or not services.is_payable(invoice):
            raise RpcError(ERR_INVALID_ACCOUNT, self.account_field)
        amount = params.get("amount")
        if not isinstance(amount, int | float) or int(amount) != to_tiyin(invoice.total):
            raise RpcError(ERR_INVALID_AMOUNT, "amount")
        return invoice

    def _transaction(self, params: dict[str, Any]) -> Transaction:
        tx = Transaction.objects.filter(provider=self.record, external_id=str(params["id"])).first()
        if tx is None:
            raise RpcError(ERR_TRANSACTION_NOT_FOUND, "id")
        return tx

    def _expire_if_stale(self, tx: Transaction) -> Transaction:
        if tx.provider_state == STATE_CREATED and tx.is_older_than(TRANSACTION_TIMEOUT):
            tx = services.cancel_transaction(
                tx, reason=REASON_TIMEOUT, provider_state=STATE_CANCELLED, error="timeout"
            )
        return tx

    @staticmethod
    def _cancel_time(tx: Transaction) -> int:
        return epoch_ms(tx.cancelled_at)

    # ------------------------------------------------------------ methods ---
    def check_perform_transaction(self, params: dict[str, Any]) -> dict[str, Any]:
        self._payable_invoice(params)
        return {"allow": True}

    def create_transaction(self, params: dict[str, Any]) -> dict[str, Any]:
        external_id = str(params["id"])
        tx = Transaction.objects.filter(provider=self.record, external_id=external_id).first()
        if tx is not None:
            tx = self._expire_if_stale(tx)
            if tx.provider_state != STATE_CREATED:
                raise RpcError(ERR_CANNOT_PERFORM, "state")
            return {
                "create_time": epoch_ms(tx.created_at),
                "transaction": str(tx.pk),
                "state": tx.provider_state,
            }

        invoice = self._payable_invoice(params)
        busy = Transaction.objects.filter(
            provider=self.record,
            invoice=invoice,
            provider_state=STATE_CREATED,
            status=Transaction.Status.CREATED,
        )
        for other in busy:
            if self._expire_if_stale(other).status == Transaction.Status.CREATED:
                raise RpcError(ERR_ORDER_BUSY, self.account_field)
        tx = services.open_transaction(
            self.record,
            invoice,
            external_id=external_id,
            provider_state=STATE_CREATED,
            provider_time=int(params["time"]),
            raw={"create": params},
        )
        return {
            "create_time": epoch_ms(tx.created_at),
            "transaction": str(tx.pk),
            "state": STATE_CREATED,
        }

    def perform_transaction(self, params: dict[str, Any]) -> dict[str, Any]:
        tx = self._expire_if_stale(self._transaction(params))
        if tx.provider_state == STATE_CREATED:
            try:
                tx = services.complete_transaction(tx, provider_state=STATE_PERFORMED)
            except BillingError as exc:
                logger.warning("Payme perform rejected: %s", exc, extra={"tx": tx.pk})
                raise RpcError(ERR_CANNOT_PERFORM, str(exc)) from exc
        elif tx.provider_state != STATE_PERFORMED:
            raise RpcError(ERR_CANNOT_PERFORM, "state")
        return {
            "transaction": str(tx.pk),
            "perform_time": epoch_ms(tx.performed_at),
            "state": STATE_PERFORMED,
        }

    def cancel_transaction(self, params: dict[str, Any]) -> dict[str, Any]:
        tx = self._transaction(params)
        if tx.provider_state == STATE_CREATED:
            tx = services.cancel_transaction(
                tx, reason=int(params.get("reason") or 0), provider_state=STATE_CANCELLED
            )
        elif tx.provider_state == STATE_PERFORMED:
            raise RpcError(ERR_CANNOT_CANCEL, "state")
        return {
            "transaction": str(tx.pk),
            "cancel_time": self._cancel_time(tx),
            "state": tx.provider_state,
        }

    def check_transaction(self, params: dict[str, Any]) -> dict[str, Any]:
        tx = self._transaction(params)
        return {
            "create_time": epoch_ms(tx.created_at),
            "perform_time": epoch_ms(tx.performed_at),
            "cancel_time": self._cancel_time(tx),
            "transaction": str(tx.pk),
            "state": tx.provider_state,
            "reason": tx.cancel_reason,
        }

    def get_statement(self, params: dict[str, Any]) -> dict[str, Any]:
        start = datetime.fromtimestamp(int(params["from"]) / 1000, tz=UTC)
        end = datetime.fromtimestamp(int(params["to"]) / 1000, tz=UTC)
        rows = (
            Transaction.objects.filter(
                provider=self.record, created_at__gte=start, created_at__lte=end
            )
            .exclude(external_id="")
            .exclude(provider_state__isnull=True)
            .order_by("created_at")
        )
        return {
            "transactions": [
                {
                    "id": tx.external_id,
                    "time": tx.provider_time,
                    "amount": tx.amount_tiyin,
                    "account": {self.account_field: tx.order_id},
                    "create_time": epoch_ms(tx.created_at),
                    "perform_time": epoch_ms(tx.performed_at),
                    "cancel_time": self._cancel_time(tx),
                    "transaction": str(tx.pk),
                    "state": tx.provider_state,
                    "reason": tx.cancel_reason,
                }
                for tx in rows
            ]
        }

    def change_password(self, params: dict[str, Any]) -> dict[str, Any]:
        password = params.get("password")
        if not isinstance(password, str) or not password:
            raise RpcError(ERR_INVALID_PARAMS, "password")
        self.record.config = {**self.config, "secret_key": password}
        self.record.save(update_fields=["config", "updated_at"])
        logger.info("Payme cashbox key rotated via ChangePassword")
        return {"success": True}
