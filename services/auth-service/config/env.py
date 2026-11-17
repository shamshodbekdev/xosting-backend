"""Tiny typed helpers for reading environment variables."""

import os

from django.core.exceptions import ImproperlyConfigured

_MISSING = object()


def env(name: str, default: object = _MISSING) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        if default is _MISSING:
            raise ImproperlyConfigured(f"Environment variable {name} is required")
        return default  # type: ignore[return-value]
    return value


def env_bool(name: str, default: bool) -> bool:
    return env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    return int(env(name, str(default)))


def env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]
