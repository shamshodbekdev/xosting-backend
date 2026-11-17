"""Batteries for FastAPI services: app factory pieces shared by every service.

* ``install_observability`` - request id, access logs, Prometheus, tracing
* ``install_error_handlers`` - uniform error envelope (same shape as Django services)
* ``health_router`` - ``/healthz``, ``/readyz`` and ``/metrics``
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from xosting_shared.logger import get_request_id
from xosting_shared.logger.asgi import RequestContextMiddleware
from xosting_shared.metrics.asgi import PrometheusMiddleware, metrics_endpoint
from xosting_shared.tracing import instrument_fastapi

__all__ = ["ReadinessCheck", "health_router", "install_error_handlers", "install_observability"]

logger = logging.getLogger(__name__)

ReadinessCheck = Callable[[Request], Awaitable[None]]

_STATUS_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "throttled",
}


def _envelope(
    code: str, message: str, details: object = None, request: Request | None = None
) -> dict:
    # Exception handlers for unhandled errors run outside the request-context
    # middleware, so fall back to the inbound header.
    request_id = get_request_id() or (request.headers.get("x-request-id") if request else None)
    return {
        "error": {"code": code, "message": message, "details": details},
        "request_id": request_id,
    }


def install_observability(app: FastAPI, service_name: str) -> None:
    # Middleware added last runs first: request context must wrap everything.
    app.add_middleware(PrometheusMiddleware)
    app.add_middleware(RequestContextMiddleware)
    instrument_fastapi(app, service_name)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        return JSONResponse(
            _envelope(code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        return JSONResponse(
            _envelope("validation_error", "Request validation failed.", details), status_code=422
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", exc_info=exc)
        return JSONResponse(
            _envelope("internal_error", "Internal server error.", request=request), status_code=500
        )


def health_router(checks: dict[str, ReadinessCheck]) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        results: dict[str, str] = {}
        for name, check in checks.items():
            try:
                await check(request)
                results[name] = "ok"
            except Exception:
                logger.exception("readiness check failed", extra={"check": name})
                results[name] = "unavailable"
        ready = all(v == "ok" for v in results.values())
        return JSONResponse(
            {"status": "ok" if ready else "unavailable", "checks": results},
            status_code=200 if ready else 503,
        )

    router.add_api_route("/metrics", metrics_endpoint, methods=["GET"])
    return router
