"""Optional OpenTelemetry tracing.

Tracing is enabled only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set and the
``tracing`` extra is installed, so services start fine without a collector.
Spans are exported over OTLP/HTTP (Jaeger, Tempo, Honeycomb, ...).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["instrument_django", "instrument_fastapi", "setup_tracing"]


def setup_tracing(service_name: str, service_version: str = "0.1.0") -> bool:
    """Configure the global tracer provider. Returns ``True`` when enabled."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("OTEL endpoint configured but opentelemetry packages are missing")
        return False

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment": os.environ.get("ENVIRONMENT", "development"),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    logger.info("tracing enabled", extra={"otlp_endpoint": endpoint})
    return True


def instrument_fastapi(app: Any, service_name: str) -> None:
    if not setup_tracing(service_name):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.warning("opentelemetry-instrumentation-fastapi is not installed")
        return
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")


def instrument_django(service_name: str) -> None:
    if not setup_tracing(service_name):
        return
    try:
        from opentelemetry.instrumentation.django import DjangoInstrumentor
    except ImportError:
        logger.warning("opentelemetry-instrumentation-django is not installed")
        return
    DjangoInstrumentor().instrument(excluded_urls="healthz,readyz,metrics")
