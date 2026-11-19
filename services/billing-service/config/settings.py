"""billing-service settings. All configuration comes from the environment."""

from pathlib import Path

import dj_database_url
from celery.schedules import crontab
from xosting_shared.logger import django_logging_config

from .env import env, env_bool, env_int, env_list

BASE_DIR = Path(__file__).resolve().parent.parent
SERVICE_NAME = "billing-service"

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["*"] if DEBUG else [])
ENVIRONMENT = env("ENVIRONMENT", "development")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "apps.core",
    "apps.billing",
    "apps.payments",
]

MIDDLEWARE = [
    "xosting_shared.logger.django.RequestContextMiddleware",
    "xosting_shared.metrics.django.PrometheusMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": dj_database_url.parse(
        env("DATABASE_URL"), conn_max_age=env_int("DB_CONN_MAX_AGE", 60), conn_health_checks=True
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Tashkent"
USE_I18N = True
USE_TZ = True

# --- Django admin (payment providers, served behind the gateway) ---------------
STATIC_URL = "/api/v1/billing/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", [])

# --- REST framework: stateless JWT validation (tokens issued by auth-service) --
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTStatelessUserAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 25,
    "EXCEPTION_HANDLER": "apps.core.exceptions.exception_handler",
}

SIMPLE_JWT = {
    "ALGORITHM": "HS256",
    "SIGNING_KEY": env("JWT_SECRET"),
    "ISSUER": env("JWT_ISSUER", "xosting-auth"),
    "AUDIENCE": env("JWT_AUDIENCE", "xosting-api"),
    "USER_ID_CLAIM": "sub",
    "LEEWAY": 10,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "xosting.net Billing Service",
    "DESCRIPTION": "Plans, subscriptions, invoices and payments.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --- Redis / Celery --------------------------------------------------------------
REDIS_URL = env("REDIS_URL", "redis://localhost:6379/0")
EVENTS_STREAM = env("EVENTS_STREAM", "xosting.events")

CELERY_BROKER_URL = env("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", REDIS_URL)
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_RESULT_EXPIRES = 3600
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BEAT_SCHEDULE = {
    "generate-renewal-invoices": {
        "task": "billing.generate_renewal_invoices",
        "schedule": crontab(minute=0, hour=3),
    },
    "process-overdue-invoices": {
        "task": "billing.process_overdue_invoices",
        "schedule": crontab(minute=15),
    },
    "expire-cancelled-subscriptions": {
        "task": "billing.expire_cancelled_subscriptions",
        "schedule": crontab(minute=30),
    },
    "sync-checkout-transactions": {
        "task": "payments.sync_checkout_transactions",
        "schedule": crontab(minute="*/5"),
    },
}

# --- Billing policy ---------------------------------------------------------------
BILLING_CURRENCY = env("BILLING_CURRENCY", "UZS")
BILLING_INVOICE_DUE_DAYS = env_int("BILLING_INVOICE_DUE_DAYS", 3)
BILLING_RENEWAL_LEAD_DAYS = env_int("BILLING_RENEWAL_LEAD_DAYS", 7)
BILLING_SUSPEND_GRACE_DAYS = env_int("BILLING_SUSPEND_GRACE_DAYS", 5)

# --- Payment providers ------------------------------------------------------------
# Where providers send the customer after paying (``?invoice=<number>`` is appended).
PAYMENTS_RETURN_URL = env("PAYMENTS_RETURN_URL", "https://xosting.net/billing/invoices")
# Public origin of the API gateway; used to build provider webhook URLs.
PAYMENTS_PUBLIC_BASE_URL = env("PAYMENTS_PUBLIC_BASE_URL", "https://api.xosting.net")

LOGGING = django_logging_config(SERVICE_NAME, env("LOG_LEVEL", "INFO"))
