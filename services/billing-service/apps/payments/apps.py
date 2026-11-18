from django.apps import AppConfig
from django.db.models.signals import post_migrate


class PaymentsConfig(AppConfig):
    name = "apps.payments"
    label = "payments"
    verbose_name = "Payment Providers"

    def ready(self) -> None:
        from .registry import sync_providers

        post_migrate.connect(sync_providers, sender=self, dispatch_uid="payments.sync_providers")
