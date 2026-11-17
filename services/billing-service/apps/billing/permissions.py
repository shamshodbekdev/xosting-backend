from rest_framework.permissions import BasePermission


class IsStaff(BasePermission):
    """Staff/admin role carried in the JWT (``is_staff`` claim)."""

    def has_permission(self, request, view) -> bool:
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)
