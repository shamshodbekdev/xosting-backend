from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from xosting_shared.metrics.django import metrics_view

from apps.core.views import healthz, readyz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path("metrics", metrics_view, name="metrics"),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/auth/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/v1/auth/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
    path("api/v1/auth/admin/", admin.site.urls),
    path("internal/v1/", include("apps.accounts.internal_urls")),
]
