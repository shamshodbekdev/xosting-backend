import logging

from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.core.events import publish_event
from apps.core.permissions import IsInternalService

from .models import User
from .serializers import (
    ChangePasswordSerializer,
    LogoutSerializer,
    RegisterSerializer,
    UserContactSerializer,
    UserSerializer,
    issue_tokens,
)

logger = logging.getLogger(__name__)


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []
    throttle_scope = "register"

    @transaction.atomic
    def create(self, request, *args, **kwargs) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        publish_event(
            "user.registered",
            user_id=str(user.id),
            payload={"email": user.email, "first_name": user.first_name},
        )
        logger.info("user registered", extra={"user_id": str(user.id)})
        return Response(
            {"user": UserSerializer(user).data, "tokens": issue_tokens(user)},
            status=status.HTTP_201_CREATED,
        )


class LoginView(TokenObtainPairView):
    throttle_scope = "login"


class RefreshView(TokenRefreshView):
    throttle_scope = "login"


class LogoutView(APIView):
    @extend_schema(request=LogoutSerializer, responses={205: None})
    def post(self, request) -> Response:
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            token = RefreshToken(serializer.validated_data["refresh"])
            if str(token["sub"]) != str(request.user.id):
                return Response(status=status.HTTP_403_FORBIDDEN)
            token.blacklist()
        except TokenError:
            pass  # already invalid or blacklisted: logout is idempotent
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self) -> User:
        return self.request.user


class ChangePasswordView(APIView):
    throttle_scope = "password"

    @extend_schema(request=ChangePasswordSerializer, responses={204: None})
    @transaction.atomic
    def post(self, request) -> Response:
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user: User = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password", "updated_at"])

        # Revoke every refresh token so other sessions must re-authenticate.
        outstanding = OutstandingToken.objects.filter(user=user).exclude(
            blacklistedtoken__isnull=False
        )
        BlacklistedToken.objects.bulk_create(
            [BlacklistedToken(token=token) for token in outstanding], ignore_conflicts=True
        )
        publish_event("user.password_changed", user_id=str(user.id), payload={"email": user.email})
        return Response(status=status.HTTP_204_NO_CONTENT)


class InternalUserContactView(generics.RetrieveAPIView):
    """``GET /internal/v1/users/<id>/contact`` - used by notification-service."""

    queryset = User.objects.all()
    serializer_class = UserContactSerializer
    authentication_classes: list = []
    permission_classes = [IsInternalService]
    lookup_field = "id"
