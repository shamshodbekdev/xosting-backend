import logging

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import APIException, NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import Invoice

from . import services
from .models import PaymentProvider
from .providers.base import ProviderError, ProviderMisconfigured
from .registry import get_provider

logger = logging.getLogger(__name__)


def _error(code: str, message: str, http_status: int) -> JsonResponse:
    return JsonResponse({"error": {"code": code, "message": message}}, status=http_status)


@csrf_exempt
@require_POST
def webhook(request: HttpRequest, provider_key: str, operation: str | None = None) -> HttpResponse:
    """Single entry point for provider callbacks: /api/v1/payments/webhook/<provider>/."""
    record = PaymentProvider.objects.filter(key=provider_key).first()
    if record is None:
        return _error("not_found", "Unknown payment provider.", 404)
    if not record.is_active:
        logger.warning("webhook for disabled provider rejected", extra={"provider": provider_key})
        return _error("provider_disabled", "This payment provider is disabled.", 403)
    try:
        provider = get_provider(record)
    except LookupError:
        return _error("not_found", "Unknown payment provider.", 404)
    if missing := provider.missing_config():
        logger.error(
            "webhook for misconfigured provider",
            extra={"provider": provider_key, "missing": missing},
        )
        return _error("provider_misconfigured", "Payment provider is not configured.", 503)
    return provider.handle_webhook(request, operation)


class BadGateway(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_code = "provider_error"
    default_detail = "The payment provider could not process the request."


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"


class ProviderSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentProvider
        fields = ["key", "name"]


class CheckoutRequestSerializer(serializers.Serializer):
    invoice_id = serializers.UUIDField()
    provider = serializers.SlugField()


class CheckoutResponseSerializer(serializers.Serializer):
    provider = serializers.CharField()
    invoice_id = serializers.UUIDField()
    account = serializers.CharField(help_text="Reference the customer pays (invoice number).")
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    currency = serializers.CharField()
    payment_url = serializers.URLField(allow_null=True)
    transaction_id = serializers.IntegerField(allow_null=True)


class ProviderListView(APIView):
    """Payment methods the customer can choose from."""

    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []

    @extend_schema(responses=ProviderSerializer(many=True))
    def get(self, request) -> Response:
        records = PaymentProvider.objects.filter(is_active=True)
        return Response(ProviderSerializer(records, many=True).data)


class CheckoutView(APIView):
    """Start paying one of the caller's invoices with the chosen provider."""

    @extend_schema(request=CheckoutRequestSerializer, responses={201: CheckoutResponseSerializer})
    def post(self, request) -> Response:
        serializer = CheckoutRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoices = Invoice.objects.all()
        if not request.user.is_staff:
            invoices = invoices.filter(user_id=request.user.id)
        invoice = invoices.filter(pk=serializer.validated_data["invoice_id"]).first()
        if invoice is None:
            raise NotFound("Invoice not found.")

        provider_key = serializer.validated_data["provider"]
        try:
            session = services.start_checkout(invoice, provider_key)
        except services.ProviderUnavailable as exc:
            raise NotFound(str(exc)) from exc
        except services.PaymentError as exc:
            raise Conflict(str(exc)) from exc
        except ProviderMisconfigured as exc:
            logger.error("checkout with misconfigured provider: %s", exc)
            raise BadGateway("Payment provider is not configured.") from exc
        except ProviderError as exc:
            logger.warning("checkout failed: %s", exc)
            raise BadGateway() from exc

        data = {
            "provider": provider_key,
            "invoice_id": invoice.pk,
            "account": session.account,
            "amount": invoice.total,
            "currency": invoice.currency,
            "payment_url": session.payment_url,
            "transaction_id": session.transaction.pk if session.transaction else None,
        }
        return Response(CheckoutResponseSerializer(data).data, status=status.HTTP_201_CREATED)
