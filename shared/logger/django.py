"""Django middleware that propagates ``X-Request-ID`` and writes access logs."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from . import request_id_var

_QUIET_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})
access_logger = logging.getLogger("http.access")


class RequestContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            if request.path not in _QUIET_PATHS:
                access_logger.info(
                    "%s %s %s",
                    request.method,
                    request.path,
                    response.status_code,
                    extra={
                        "http_method": request.method,
                        "http_path": request.path,
                        "http_status": response.status_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
            return response
        finally:
            request_id_var.reset(token)
