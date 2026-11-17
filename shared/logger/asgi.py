"""ASGI middleware that propagates ``X-Request-ID`` and writes access logs."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from . import request_id_var

REQUEST_ID_HEADER = b"x-request-id"
_QUIET_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})

access_logger = logging.getLogger("http.access")


class RequestContextMiddleware:
    """Pure ASGI middleware (works for HTTP and WebSocket scopes)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        request_id = headers.get(REQUEST_ID_HEADER, b"").decode("latin-1") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [
                    *message["headers"],
                    (REQUEST_ID_HEADER, request_id.encode("latin-1")),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if scope["type"] == "http" and path not in _QUIET_PATHS:
                access_logger.info(
                    "%s %s %s",
                    scope.get("method"),
                    path,
                    status_code,
                    extra={
                        "http_method": scope.get("method"),
                        "http_path": path,
                        "http_status": status_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
            request_id_var.reset(token)
