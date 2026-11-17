from rest_framework import serializers

from .models import Invoice, InvoiceLine, Payment, Plan, Subscription


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = [
            "code",
            "name",
            "service_type",
            "description",
            "price",
            "currency",
            "period_months",
            "features",
        ]


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            "id",
            "user_id",
            "plan",
            "status",
            "auto_renew",
            "current_period_start",
            "current_period_end",
            "cancelled_at",
            "resource_ref",
            "created_at",
        ]
        read_only_fields = fields


class SubscribeSerializer(serializers.Serializer):
    plan_code = serializers.SlugField()

    def validate_plan_code(self, value: str) -> Plan:
        try:
            return Plan.objects.get(code=value, is_active=True)
        except Plan.DoesNotExist as exc:
            raise serializers.ValidationError("Unknown or inactive plan.") from exc


class InvoiceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLine
        fields = ["description", "quantity", "unit_price", "amount"]


class InvoiceSerializer(serializers.ModelSerializer):
    lines = InvoiceLineSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = [
            "id",
            "number",
            "user_id",
            "subscription",
            "kind",
            "status",
            "currency",
            "total",
            "period_start",
            "period_end",
            "issued_at",
            "due_at",
            "paid_at",
            "lines",
        ]
        read_only_fields = fields


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            "id",
            "invoice",
            "amount",
            "currency",
            "provider",
            "provider_transaction_id",
            "status",
            "created_at",
        ]
        read_only_fields = fields


class RecordPaymentSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    provider = serializers.ChoiceField(choices=Payment.Provider.choices)
    provider_transaction_id = serializers.CharField(max_length=128, required=False, default="")
