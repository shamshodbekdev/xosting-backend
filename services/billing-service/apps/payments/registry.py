"""Maps provider keys to their implementation classes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import PaymentProvider
    from .providers.base import BaseProvider

_REGISTRY: dict[str, type[BaseProvider]] = {}


def register(cls: type[BaseProvider]) -> type[BaseProvider]:
    if cls.key in _REGISTRY and _REGISTRY[cls.key] is not cls:
        raise RuntimeError(f"Payment provider '{cls.key}' is registered twice")
    _REGISTRY[cls.key] = cls
    return cls


def provider_classes() -> dict[str, type[BaseProvider]]:
    from . import providers  # noqa: F401  (imports every implementation)

    return dict(_REGISTRY)


def get_provider(record: PaymentProvider) -> BaseProvider:
    try:
        cls = provider_classes()[record.key]
    except KeyError as exc:
        raise LookupError(f"No implementation for payment provider '{record.key}'") from exc
    return cls(record)


def sync_providers(**_kwargs) -> None:
    """Ensure a (disabled) PaymentProvider row exists for every implementation."""
    from .models import PaymentProvider

    for key, cls in provider_classes().items():
        PaymentProvider.objects.get_or_create(
            key=key,
            defaults={"name": cls.name, "is_active": False, "sort_order": cls.sort_order},
        )
