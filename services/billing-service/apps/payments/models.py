from datetime import timedelta
from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.utils import timezone


class PaymentProvider(models.Model):
    """A payment gateway that can be switched on or off from the admin.

    One row exists per provider implementation (rows are created automatically
    after ``migrate``, disabled by default). Credentials live in ``config``;
    see the provider module for the keys it expects.
    """

    key = models.SlugField(max_length=32, unique=True)
    name = models.CharField(max_length=64)
    is_active = models.BooleanField(
        default=False, help_text="Disabled providers reject webhooks with HTTP 403."
    )
    config = models.JSONField(default=dict, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "payment provider"
        verbose_name_plural = "payment providers"

    def __str__(self) -> str:
        return self.name


class Transaction(models.Model):
    """One payment attempt for an invoice through a provider."""

    class Status(models.TextChoices):
        CREATED = "created", "Created"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    provider = models.ForeignKey(
        PaymentProvider, on_delete=models.PROTECT, related_name="transactions"
    )
    invoice = models.ForeignKey(
        "billing.Invoice", on_delete=models.PROTECT, related_name="provider_transactions"
    )
    order_id = models.CharField(
        max_length=64, db_index=True, help_text="Merchant reference sent to the provider."
    )
    external_id = models.CharField(
        max_length=128, blank=True, help_text="Transaction identifier assigned by the provider."
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="UZS")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CREATED)
    provider_state = models.SmallIntegerField(
        null=True, blank=True, help_text="Provider-specific state code (e.g. Payme state)."
    )
    provider_time = models.BigIntegerField(
        null=True, blank=True, help_text="Creation time reported by the provider (epoch ms)."
    )
    cancel_reason = models.SmallIntegerField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    payment = models.OneToOneField(
        "billing.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="provider_transaction",
    )
    performed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["provider", "status", "created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_id"],
                condition=~Q(external_id=""),
                name="uniq_provider_external_id",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider_id}:{self.external_id or self.pk} [{self.status}]"

    @property
    def amount_tiyin(self) -> int:
        return to_tiyin(self.amount)

    def is_older_than(self, age: timedelta) -> bool:
        return timezone.now() - self.created_at > age


def to_tiyin(amount: Decimal) -> int:
    """so'm (2 decimal places) -> integer tiyin."""
    return int((Decimal(amount) * 100).to_integral_value())


def from_tiyin(value: int | str) -> Decimal:
    return (Decimal(value) / 100).quantize(Decimal("0.01"))


def epoch_ms(moment) -> int:
    return int(moment.timestamp() * 1000) if moment else 0
