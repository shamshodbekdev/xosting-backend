import uuid
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ServiceType(models.TextChoices):
    HOSTING = "hosting", "Shared hosting"
    VPS = "vps", "VPS"
    DOMAIN = "domain", "Domain"
    SSL = "ssl", "SSL certificate"


class Plan(TimestampedModel):
    class Period(models.IntegerChoices):
        MONTHLY = 1, "Monthly"
        QUARTERLY = 3, "Quarterly"
        SEMIANNUAL = 6, "Semi-annual"
        ANNUAL = 12, "Annual"

    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    service_type = models.CharField(max_length=16, choices=ServiceType.choices)
    description = models.TextField(blank=True)
    price = models.DecimalField(
        max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    currency = models.CharField(max_length=3, default="UZS")
    period_months = models.PositiveSmallIntegerField(choices=Period.choices, default=Period.MONTHLY)
    features = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["service_type", "sort_order", "price"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class Subscription(TimestampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending payment"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        SUSPENDED = "suspended", "Suspended"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.UUIDField(db_index=True)
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    auto_renew = models.BooleanField(default=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    resource_ref = models.CharField(
        max_length=128, blank=True, help_text="Identifier of the provisioned resource."
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "current_period_end"])]

    def __str__(self) -> str:
        return f"{self.plan.code} for {self.user_id} [{self.status}]"


class InvoiceSequence(models.Model):
    """Gap-free, per-year invoice numbering (row-locked during allocation)."""

    year = models.PositiveSmallIntegerField(primary_key=True)
    last_value = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.year}: {self.last_value}"


class Invoice(TimestampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        OVERDUE = "overdue", "Overdue"
        PAID = "paid", "Paid"
        VOID = "void", "Void"

    class Kind(models.TextChoices):
        NEW = "new", "New subscription"
        RENEWAL = "renewal", "Renewal"

    PAYABLE_STATUSES = (Status.OPEN, Status.OVERDUE)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=32, unique=True)
    user_id = models.UUIDField(db_index=True)
    subscription = models.ForeignKey(
        Subscription, on_delete=models.PROTECT, related_name="invoices", null=True, blank=True
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.NEW)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    currency = models.CharField(max_length=3)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField()
    due_at = models.DateTimeField()
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-issued_at"]
        indexes = [models.Index(fields=["status", "due_at"])]
        constraints = [
            # At most one renewal invoice per subscription billing period.
            models.UniqueConstraint(
                fields=["subscription", "period_start"],
                condition=Q(kind="renewal") & ~Q(status="void"),
                name="uniq_renewal_invoice_per_period",
            ),
        ]

    def __str__(self) -> str:
        return self.number


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    def __str__(self) -> str:
        return self.description


class Payment(models.Model):
    class Provider(models.TextChoices):
        MANUAL = "manual", "Manual (staff)"
        PAYME = "payme", "Payme"
        CLICK = "click", "Click"
        UZUM = "uzum", "Uzum"

    class Status(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        REFUNDED = "refunded", "Refunded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    user_id = models.UUIDField(db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3)
    provider = models.CharField(max_length=16, choices=Provider.choices)
    provider_transaction_id = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SUCCEEDED)
    idempotency_key = models.CharField(max_length=128, unique=True)
    recorded_by = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_transaction_id"],
                condition=~Q(provider_transaction_id=""),
                name="uniq_provider_transaction",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.amount} {self.currency} via {self.provider}"
