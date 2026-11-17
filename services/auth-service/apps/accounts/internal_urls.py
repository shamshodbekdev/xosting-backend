from django.urls import path

from . import views

app_name = "internal"

urlpatterns = [
    path("users/<uuid:id>/contact", views.InternalUserContactView.as_view(), name="user-contact"),
]
