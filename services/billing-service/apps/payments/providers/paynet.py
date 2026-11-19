"""Paynet (Uzbekistan) provider web service — SOAP 1.1.

Paynet POSTs a SOAP envelope whose body holds one ``<Method>Arguments``
element (``GetInformation``, ``PerformTransaction``, ``CheckTransaction``,
``CancelTransaction``, ``GetStatement``). Credentials (``username``,
``password``) and ``serviceId`` travel inside that element. The answer is a
``<ns2:<Method>Result>`` element in the ``http://uws.provider.com/``
namespace with ``errorMsg``, ``status`` (0 = success) and ``timeStamp``.

The customer enters the invoice number as the account parameter.
PerformTransaction credits the invoice immediately. Once credited, a payment
cannot be cancelled automatically (status 77); refunds are handled by staff.

config keys:
    username       login Paynet uses to call us                    required
    password       password Paynet uses to call us                 required
    service_id     expected serviceId (optional, checked if set)
    account_param  parameter name holding the invoice number (default "account")
    amount_unit    "tiyin" (default) or "sum" - unit of the amount field
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from xml.etree.ElementTree import Element, SubElement, tostring  # noqa: S405  (output only)

from defusedxml import ElementTree as SafeET
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from apps.billing.models import Invoice
from apps.billing.services import BillingError

from .. import services
from ..models import Transaction, from_tiyin, to_tiyin
from ..registry import register
from .base import BaseProvider, CheckoutSession

logger = logging.getLogger(__name__)

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
UWS_NS = "http://uws.provider.com/"

OK = 0
CANCEL_NOT_ALLOWED = 77
SYSTEM_ERROR = 102
TRANSACTION_EXISTS = 201
TRANSACTION_CANCELLED = 202
TRANSACTION_NOT_FOUND = 203
ACCOUNT_NOT_FOUND = 302
SERVICE_NOT_FOUND = 304
MISSING_PARAMS = 411
INVALID_LOGIN = 412
INVALID_AMOUNT = 413
INVALID_DATE = 414
UNKNOWN_METHOD = 603

MESSAGES = {
    OK: "Success",
    CANCEL_NOT_ALLOWED: "Payment is already applied and cannot be cancelled",
    SYSTEM_ERROR: "System error",
    TRANSACTION_EXISTS: "Transaction already exists",
    TRANSACTION_CANCELLED: "Transaction already cancelled",
    TRANSACTION_NOT_FOUND: "Transaction not found",
    ACCOUNT_NOT_FOUND: "Invoice not found or cannot be paid",
    SERVICE_NOT_FOUND: "Service not found",
    MISSING_PARAMS: "Required parameters are missing",
    INVALID_LOGIN: "Invalid login or password",
    INVALID_AMOUNT: "Invalid amount",
    INVALID_DATE: "Invalid date format",
    UNKNOWN_METHOD: "Unknown method",
}

# Paynet transactionState values.
STATE_PERFORMED = 1
STATE_CANCELLED = 2

METHODS = {
    "GetInformation": "get_information",
    "PerformTransaction": "perform_transaction",
    "CheckTransaction": "check_transaction",
    "CancelTransaction": "cancel_transaction",
    "GetStatement": "get_statement",
}


class PaynetError(Exception):
    def __init__(self, status: int, **fields: str | int):
        super().__init__(status)
        self.status = status
        self.fields = fields


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _child_text(element: Element, name: str) -> str | None:
    for child in element:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return None


def _parameters(element: Element) -> dict[str, str]:
    params: dict[str, str] = {}
    for child in element:
        if _local(child.tag) != "parameters":
            continue
        key = _child_text(child, "paramKey")
        if key:
            params[key] = _child_text(child, "paramValue") or ""
    return params


def _parse_time(value: str | None) -> datetime:
    if not value:
        raise PaynetError(INVALID_DATE)
    try:
        if value.isdigit():
            return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise PaynetError(INVALID_DATE) from exc
    return moment if moment.tzinfo else timezone.make_aware(moment)


def _format_time(moment: datetime) -> str:
    return timezone.localtime(moment).isoformat(timespec="seconds")


@register
class PaynetProvider(BaseProvider):
    key = "paynet"
    name = "Paynet"
    sort_order = 40
    required_config = ("username", "password")

    def create_checkout(self, invoice: Invoice, return_url: str) -> CheckoutSession:
        # Paid in the Paynet app or at a Paynet terminal using the invoice number.
        return CheckoutSession(payment_url=None, account=invoice.number)

    # ------------------------------------------------------------ amounts ---
    def _amount_from_wire(self, raw: str | None) -> Decimal:
        try:
            value = Decimal(raw or "")
        except InvalidOperation as exc:
            raise PaynetError(INVALID_AMOUNT) from exc
        if self.setting("amount_unit", "tiyin") == "sum":
            return value.quantize(Decimal("0.01"))
        return from_tiyin(value)

    def _amount_to_wire(self, amount: Decimal) -> str:
        if self.setting("amount_unit", "tiyin") == "sum":
            return f"{amount:.2f}"
        return str(to_tiyin(amount))

    # ------------------------------------------------------------ webhook ---
    def handle_webhook(self, request: HttpRequest, operation: str | None) -> HttpResponse:
        try:
            root = SafeET.fromstring(request.body)
        except Exception:  # defusedxml raises ParseError and its own security errors
            return self._fault("Malformed SOAP request")
        body = next((el for el in root if _local(el.tag) == "Body"), None)
        arguments = next(iter(body), None) if body is not None else None
        if arguments is None or not _local(arguments.tag).endswith("Arguments"):
            return self._fault("SOAP body has no *Arguments element")

        method = _local(arguments.tag)[: -len("Arguments")]
        try:
            self._authenticate(arguments)
            if method not in METHODS:
                raise PaynetError(UNKNOWN_METHOD)
            handler = getattr(self, METHODS[method])
            status, fields = OK, handler(arguments)
        except PaynetError as exc:
            status, fields = exc.status, exc.fields
        return self._respond(method, status, fields)

    def _authenticate(self, arguments: Element) -> None:
        username = _child_text(arguments, "username") or ""
        password = _child_text(arguments, "password") or ""
        valid_user = hmac.compare_digest(username.encode(), str(self.setting("username")).encode())
        valid_pass = hmac.compare_digest(password.encode(), str(self.setting("password")).encode())
        if not (valid_user and valid_pass):
            raise PaynetError(INVALID_LOGIN)
        expected_service = self.config.get("service_id")
        if expected_service and _child_text(arguments, "serviceId") != str(expected_service):
            raise PaynetError(SERVICE_NOT_FOUND)

    def _respond(self, method: str, status: int, fields: dict) -> HttpResponse:
        envelope = Element("soapenv:Envelope", {"xmlns:soapenv": SOAP_NS})
        body = SubElement(envelope, "soapenv:Body")
        result = SubElement(body, f"ns2:{method}Result", {"xmlns:ns2": UWS_NS})
        SubElement(result, "errorMsg").text = MESSAGES.get(status, MESSAGES[SYSTEM_ERROR])
        SubElement(result, "status").text = str(status)
        SubElement(result, "timeStamp").text = _format_time(timezone.now())
        for name, value in fields.items():
            if name == "parameters":
                for key, item in value.items():
                    param = SubElement(result, "parameters")
                    SubElement(param, "paramKey").text = key
                    SubElement(param, "paramValue").text = str(item)
            elif name == "statements":
                for row in value:
                    statement = SubElement(result, "statements")
                    for key, item in row.items():
                        SubElement(statement, key).text = str(item)
            else:
                SubElement(result, name).text = str(value)
        xml = tostring(envelope, encoding="utf-8", xml_declaration=True)
        return HttpResponse(xml, content_type="text/xml; charset=utf-8")

    @staticmethod
    def _fault(message: str) -> HttpResponse:
        envelope = Element("soapenv:Envelope", {"xmlns:soapenv": SOAP_NS})
        fault = SubElement(SubElement(envelope, "soapenv:Body"), "soapenv:Fault")
        SubElement(fault, "faultcode").text = "soapenv:Client"
        SubElement(fault, "faultstring").text = message
        xml = tostring(envelope, encoding="utf-8", xml_declaration=True)
        return HttpResponse(xml, status=400, content_type="text/xml; charset=utf-8")

    # ------------------------------------------------------------ helpers ---
    def _account(self, arguments: Element) -> str:
        params = _parameters(arguments)
        account = params.get(str(self.setting("account_param", "account")))
        if account is None and len(params) == 1:
            account = next(iter(params.values()))
        if not account:
            raise PaynetError(MISSING_PARAMS)
        return account

    def _transaction(self, arguments: Element) -> Transaction:
        transaction_id = _child_text(arguments, "transactionId")
        if not transaction_id:
            raise PaynetError(MISSING_PARAMS)
        tx = Transaction.objects.filter(provider=self.record, external_id=transaction_id).first()
        if tx is None:
            raise PaynetError(TRANSACTION_NOT_FOUND)
        return tx

    # ------------------------------------------------------------ methods ---
    def get_information(self, arguments: Element) -> dict:
        invoice = services.find_invoice(self._account(arguments))
        if invoice is None or not services.is_payable(invoice):
            raise PaynetError(ACCOUNT_NOT_FOUND)
        return {
            "parameters": {
                "invoice": invoice.number,
                "amount": self._amount_to_wire(invoice.total),
                "currency": invoice.currency,
            }
        }

    def perform_transaction(self, arguments: Element) -> dict:
        transaction_id = _child_text(arguments, "transactionId")
        if not transaction_id:
            raise PaynetError(MISSING_PARAMS)
        existing = Transaction.objects.filter(
            provider=self.record, external_id=transaction_id
        ).first()
        if existing is not None:
            raise PaynetError(TRANSACTION_EXISTS, providerTrnId=existing.pk)

        transaction_time = _parse_time(_child_text(arguments, "transactionTime"))
        amount = self._amount_from_wire(_child_text(arguments, "amount"))
        invoice = services.find_invoice(self._account(arguments))
        if invoice is None or not services.is_payable(invoice):
            raise PaynetError(ACCOUNT_NOT_FOUND)
        if amount != invoice.total:
            raise PaynetError(INVALID_AMOUNT)

        tx = services.open_transaction(
            self.record,
            invoice,
            external_id=transaction_id,
            provider_time=int(transaction_time.timestamp() * 1000),
            raw={"perform": {"transactionId": transaction_id, "amount": str(amount)}},
        )
        try:
            tx = services.complete_transaction(tx, provider_state=STATE_PERFORMED)
        except BillingError as exc:
            services.fail_transaction(tx, str(exc))
            raise PaynetError(SYSTEM_ERROR) from exc
        return {"providerTrnId": tx.pk}

    def check_transaction(self, arguments: Element) -> dict:
        tx = self._transaction(arguments)
        if tx.status == Transaction.Status.PAID:
            state, state_status = STATE_PERFORMED, OK
        elif tx.status == Transaction.Status.CANCELLED:
            state, state_status = STATE_CANCELLED, OK
        else:
            state, state_status = STATE_CANCELLED, SYSTEM_ERROR
        return {
            "providerTrnId": tx.pk,
            "transactionState": state,
            "transactionStateErrorStatus": state_status,
            "transactionStateErrorMsg": MESSAGES[state_status],
        }

    def cancel_transaction(self, arguments: Element) -> dict:
        tx = self._transaction(arguments)
        if tx.status == Transaction.Status.CANCELLED:
            raise PaynetError(TRANSACTION_CANCELLED, transactionState=STATE_CANCELLED)
        if tx.status == Transaction.Status.PAID:
            raise PaynetError(CANCEL_NOT_ALLOWED, transactionState=STATE_PERFORMED)
        tx = services.cancel_transaction(tx, provider_state=STATE_CANCELLED)
        return {"transactionState": STATE_CANCELLED}

    def get_statement(self, arguments: Element) -> dict:
        start = _parse_time(_child_text(arguments, "dateFrom"))
        end = _parse_time(_child_text(arguments, "dateTo"))
        rows = Transaction.objects.filter(
            provider=self.record,
            status=Transaction.Status.PAID,
            performed_at__gte=start,
            performed_at__lte=end,
        ).order_by("performed_at")
        return {
            "statements": [
                {
                    "amount": self._amount_to_wire(tx.amount),
                    "providerTrnId": tx.pk,
                    "transactionId": tx.external_id,
                    "transactionTime": _format_time(
                        datetime.fromtimestamp(tx.provider_time / 1000, tz=UTC)
                        if tx.provider_time
                        else tx.performed_at
                    ),
                }
                for tx in rows
            ]
        }
