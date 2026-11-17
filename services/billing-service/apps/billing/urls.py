from rest_framework.routers import SimpleRouter

from . import views

app_name = "billing"

router = SimpleRouter(trailing_slash=False)
router.register("plans", views.PlanViewSet, basename="plan")
router.register("subscriptions", views.SubscriptionViewSet, basename="subscription")
router.register("invoices", views.InvoiceViewSet, basename="invoice")

urlpatterns = router.urls
