import hmac

from django.conf import settings
from rest_framework.permissions import BasePermission


class IsInternalService(BasePermission):
    """Authorise service-to-service calls via a shared ``X-Internal-Token``."""

    message = "Internal endpoint."

    def has_permission(self, request, view) -> bool:
        supplied = request.headers.get("X-Internal-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, settings.INTERNAL_API_TOKEN)
