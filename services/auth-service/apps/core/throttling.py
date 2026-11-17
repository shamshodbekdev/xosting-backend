"""Throttling that degrades gracefully when the cache backend is unavailable.

Rate limits are a protective control, not a correctness one: if Redis is
down we log and allow the request instead of turning every login into a 500
(the API gateway still enforces its own per-IP limits).
"""

import logging

from rest_framework.throttling import ScopedRateThrottle

logger = logging.getLogger(__name__)


class FailOpenScopedRateThrottle(ScopedRateThrottle):
    def allow_request(self, request, view) -> bool:
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.warning(
                "throttle cache unavailable; allowing request",
                extra={"scope": getattr(view, "throttle_scope", None)},
                exc_info=True,
            )
            return True
