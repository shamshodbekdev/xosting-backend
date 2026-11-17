"""Django middleware and view for Prometheus metrics."""

from __future__ import annotations

import time
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from . import HTTP_REQUESTS_IN_PROGRESS, UNMATCHED_ROUTE, observe_request, render_latest


class PrometheusMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        gauge = HTTP_REQUESTS_IN_PROGRESS.labels(method=request.method)
        gauge.inc()
        started = time.perf_counter()
        status_code = 500
        try:
            response = self.get_response(request)
            status_code = response.status_code
            return response
        finally:
            gauge.dec()
            match = getattr(request, "resolver_match", None)
            route = f"/{match.route}" if match and match.route else UNMATCHED_ROUTE
            observe_request(
                request.method or "GET", route, status_code, time.perf_counter() - started
            )


def metrics_view(_request: HttpRequest) -> HttpResponse:
    payload, content_type = render_latest()
    return HttpResponse(payload, content_type=content_type)
