"""Idempotently load the default xosting.net product catalogue."""

from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.billing.models import Plan, ServiceType

DEFAULT_PLANS = [
    {
        "code": "hosting-start",
        "name": "Start",
        "service_type": ServiceType.HOSTING,
        "price": Decimal("29000"),
        "sort_order": 1,
        "features": {"sites": 1, "disk_gb": 5, "databases": 2, "mailboxes": 5, "ssl": True},
    },
    {
        "code": "hosting-business",
        "name": "Business",
        "service_type": ServiceType.HOSTING,
        "price": Decimal("59000"),
        "sort_order": 2,
        "features": {"sites": 10, "disk_gb": 25, "databases": 20, "mailboxes": 50, "ssl": True},
    },
    {
        "code": "hosting-pro",
        "name": "Pro",
        "service_type": ServiceType.HOSTING,
        "price": Decimal("119000"),
        "sort_order": 3,
        "features": {"sites": -1, "disk_gb": 100, "databases": -1, "mailboxes": -1, "ssl": True},
    },
    {
        "code": "vps-1",
        "name": "VPS 1 vCPU / 2 GB",
        "service_type": ServiceType.VPS,
        "price": Decimal("149000"),
        "sort_order": 1,
        "features": {"vcpu": 1, "ram_gb": 2, "nvme_gb": 40},
    },
]


class Command(BaseCommand):
    help = "Create or update the default plans."

    def handle(self, *args, **options) -> None:
        for spec in DEFAULT_PLANS:
            code = spec["code"]
            _, created = Plan.objects.update_or_create(
                code=code, defaults={k: v for k, v in spec.items() if k != "code"}
            )
            self.stdout.write(f"{'created' if created else 'updated'} {code}")
