"""ASGI middleware and endpoint for Prometheus metrics (FastAPI/Starlette)."""

from __future__ import annotations

import time
from typing import Any

from . import HTTP_REQUESTS_IN_PROGRESS, UNMATCHED_ROUTE, observe_request, render_latest


class PrometheusMiddleware:
    """Record RED metrics labelled by the *route template*, never the raw path,
    to keep label cardinality bounded."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        gauge = HTTP_REQUESTS_IN_PROGRESS.labels(method=method)
        gauge.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            gauge.dec()
            route = getattr(scope.get("route"), "path", None) or UNMATCHED_ROUTE
            observe_request(method, route, status_code, time.perf_counter() - started)


async def metrics_endpoint() -> Any:
    from starlette.responses import Response

    payload, content_type = render_latest()
    return Response(payload, media_type=content_type)
