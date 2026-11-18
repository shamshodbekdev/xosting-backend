from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from xosting_shared.metrics.django import metrics_view

from apps.core.views import healthz, readyz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path("metrics", metrics_view, name="metrics"),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/payments/", include("apps.payments.urls")),
    path("api/v1/billing/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/v1/billing/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]
