"""Uniform error envelope across all services::

{"error": {"code": "validation_error", "message": "...", "details": {...}},
 "request_id": "..."}
"""

from typing import Any

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from xosting_shared.logger import get_request_id


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    code = getattr(exc, "default_code", "error")
    data = response.data
    if isinstance(data, dict) and set(data) == {"detail"}:
        message, details = str(data["detail"]), None
    else:
        message, details = "Request validation failed.", data

    response.data = {
        "error": {"code": code, "message": message, "details": details},
        "request_id": get_request_id(),
    }
    return response
