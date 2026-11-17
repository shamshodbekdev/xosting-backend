"""Kubernetes probes.

* ``/healthz`` - liveness: the process is able to serve requests.
* ``/readyz``  - readiness: dependencies (PostgreSQL, Redis) are reachable.
"""

import logging

from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

from .redis import get_redis

logger = logging.getLogger(__name__)


@require_GET
def healthz(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@require_GET
def readyz(_request: HttpRequest) -> JsonResponse:
    checks: dict[str, str] = {}
    try:
        connection.ensure_connection()
        checks["database"] = "ok"
    except Exception:
        logger.exception("readiness: database unavailable")
        checks["database"] = "unavailable"
    try:
        get_redis().ping()
        checks["redis"] = "ok"
    except Exception:
        logger.exception("readiness: redis unavailable")
        checks["redis"] = "unavailable"

    ready = all(value == "ok" for value in checks.values())
    return JsonResponse(
        {"status": "ok" if ready else "unavailable", "checks": checks},
        status=200 if ready else 503,
    )
