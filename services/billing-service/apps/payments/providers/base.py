"""Provider plumbing shared by every gateway implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from django.http import HttpRequest, HttpResponse

if TYPE_CHECKING:
    from apps.billing.models import Invoice

    from ..models import PaymentProvider, Transaction

_MISSING = object()


class ProviderError(Exception):
    """The provider could not process a request (network/API error)."""


class ProviderMisconfigured(ProviderError):
    """A required credential is missing from ``PaymentProvider.config``."""


@dataclass
class CheckoutSession:
    """What the client needs to send the customer to the provider."""

    payment_url: str | None
    account: str
    transaction: Transaction | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class BaseProvider:
    key: ClassVar[str]
    name: ClassVar[str]
    sort_order: ClassVar[int] = 0
    #: Keys that must be present in ``PaymentProvider.config``.
    required_config: ClassVar[tuple[str, ...]] = ()

    def __init__(self, record: PaymentProvider):
        self.record = record

    @property
    def config(self) -> dict[str, Any]:
        return self.record.config or {}

    def setting(self, name: str, default: Any = _MISSING) -> Any:
        value = self.config.get(name)
        if value in (None, ""):
            if default is _MISSING:
                raise ProviderMisconfigured(f"{self.key}: config key '{name}' is not set")
            return default
        return value

    def missing_config(self) -> list[str]:
        return [name for name in self.required_config if self.config.get(name) in (None, "")]

    def create_checkout(self, invoice: Invoice, return_url: str) -> CheckoutSession:
        raise NotImplementedError

    def handle_webhook(self, request: HttpRequest, operation: str | None) -> HttpResponse:
        raise NotImplementedError
