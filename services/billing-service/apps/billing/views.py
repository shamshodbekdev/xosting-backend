from django.db.models import QuerySet
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response

from . import services
from .models import Invoice, Plan, Subscription
from .permissions import IsStaff
from .serializers import (
    InvoiceSerializer,
    PaymentSerializer,
    PlanSerializer,
    RecordPaymentSerializer,
    SubscribeSerializer,
    SubscriptionSerializer,
)


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"
    default_detail = "The request conflicts with the current state of the resource."


class OwnedQuerysetMixin:
    """Customers see their own records; staff may see all (optionally ?user_id=)."""

    def scope_queryset(self, queryset: QuerySet) -> QuerySet:
        user = self.request.user
        if user.is_staff:
            user_id = self.request.query_params.get("user_id")
            return queryset.filter(user_id=user_id) if user_id else queryset
        return queryset.filter(user_id=user.id)


class PlanViewSet(viewsets.ReadOnlyModelViewSet):
    """Public product catalogue."""

    serializer_class = PlanSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []
    lookup_field = "code"
    pagination_class = None

    @extend_schema(parameters=[OpenApiParameter("service_type", str)])
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def get_queryset(self) -> QuerySet:
        queryset = Plan.objects.filter(is_active=True)
        service_type = self.request.query_params.get("service_type")
        return queryset.filter(service_type=service_type) if service_type else queryset


class SubscriptionViewSet(
    OwnedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = SubscriptionSerializer

    def get_queryset(self) -> QuerySet:
        return self.scope_queryset(Subscription.objects.select_related("plan"))

    @extend_schema(request=SubscribeSerializer, responses={201: SubscriptionSerializer})
    def create(self, request) -> Response:
        serializer = SubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            subscription, invoice = services.subscribe(
                str(request.user.id), serializer.validated_data["plan_code"]
            )
        except services.BillingError as exc:
            raise Conflict(str(exc)) from exc
        return Response(
            {
                "subscription": SubscriptionSerializer(subscription).data,
                "invoice": InvoiceSerializer(invoice).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(request=None, responses=SubscriptionSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None) -> Response:
        subscription = self.get_object()
        subscription = services.cancel_subscription(str(subscription.pk))
        return Response(SubscriptionSerializer(subscription).data)


class InvoiceViewSet(OwnedQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = InvoiceSerializer

    def get_queryset(self) -> QuerySet:
        queryset = self.scope_queryset(Invoice.objects.prefetch_related("lines"))
        status_filter = self.request.query_params.get("status")
        return queryset.filter(status=status_filter) if status_filter else queryset

    @extend_schema(
        request=RecordPaymentSerializer,
        responses={201: PaymentSerializer, 200: PaymentSerializer},
        parameters=[
            OpenApiParameter("Idempotency-Key", str, OpenApiParameter.HEADER, required=True)
        ],
    )
    @action(detail=True, methods=["post"], permission_classes=[IsStaff])
    def pay(self, request, pk=None) -> Response:
        """Record a payment (staff / payment-provider callbacks). Idempotent."""
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            raise ValidationError({"Idempotency-Key": "This header is required."})
        invoice = self.get_object()
        serializer = RecordPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = services.record_payment(
                str(invoice.pk),
                idempotency_key=idempotency_key,
                recorded_by=str(request.user.id),
                **serializer.validated_data,
            )
        except services.BillingError as exc:
            raise Conflict(str(exc)) from exc
        return Response(
            PaymentSerializer(result.payment).data,
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )
