from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("providers/", views.ProviderListView.as_view(), name="providers"),
    path("checkout/", views.CheckoutView.as_view(), name="checkout"),
    path("webhook/<slug:provider_key>/", views.webhook, name="webhook"),
    path("webhook/<slug:provider_key>/<slug:operation>/", views.webhook, name="webhook-op"),
]
