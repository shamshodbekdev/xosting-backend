"""Structured JSON logging shared by all Python services.

Every log line is a single JSON object containing at least ``timestamp``,
``level``, ``service``, ``logger`` and ``message``. The current request id and
(when OpenTelemetry is active) the trace/span ids are attached automatically,
so logs can be correlated across services in Loki/ELK.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "JsonFormatter",
    "configure_logging",
    "django_logging_config",
    "get_request_id",
    "request_id_var",
    "set_request_id",
]

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_RESERVED_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}


def set_request_id(request_id: str | None) -> None:
    request_id_var.set(request_id)


def get_request_id() -> str | None:
    return request_id_var.get()


def _trace_context() -> dict[str, str]:
    try:
        from opentelemetry import trace
    except ImportError:  # tracing extra not installed
        return {}
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return {}
    return {
        "trace_id": format(span_context.trace_id, "032x"),
        "span_id": format(span_context.span_id, "016x"),
    }


class JsonFormatter(logging.Formatter):
    """Render log records as one-line JSON documents."""

    def __init__(self, service: str = "unknown", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        payload.update(_trace_context())

        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(service: str, level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service=service))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Route server loggers through the root handler instead of their own.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error", "celery"):
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True
    # Access logs are produced by our own middleware with richer context.
    logging.getLogger("uvicorn.access").disabled = True
    # Outbound HTTP client chatter is rarely useful and may include URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def django_logging_config(service: str, level: str = "INFO") -> dict[str, Any]:
    """Return a ``LOGGING`` dict for Django settings."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {"()": "xosting_shared.logger.JsonFormatter", "service": service},
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "json",
            },
        },
        "root": {"handlers": ["stdout"], "level": level.upper()},
        "loggers": {
            "django": {"handlers": ["stdout"], "level": level.upper(), "propagate": False},
            "django.server": {"handlers": [], "propagate": False},
            "django.request": {"handlers": ["stdout"], "level": "ERROR", "propagate": False},
        },
    }
