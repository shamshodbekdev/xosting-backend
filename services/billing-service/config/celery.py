"""Celery application for asynchronous and scheduled billing jobs."""

import os

from celery import Celery
from celery.signals import setup_logging

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("billing")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@setup_logging.connect
def _configure_logging(**_kwargs) -> None:
    """Keep Celery from hijacking the root logger; use our JSON formatter."""
    from django.conf import settings
    from xosting_shared.logger import configure_logging

    configure_logging(settings.SERVICE_NAME, os.environ.get("LOG_LEVEL", "INFO"))
