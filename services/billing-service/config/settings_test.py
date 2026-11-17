"""Test settings: self-contained, no external services required."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("DATABASE_URL", "sqlite://:memory:")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-with-at-least-32-bytes!!")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

from .settings import *  # noqa: E402,F403

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
