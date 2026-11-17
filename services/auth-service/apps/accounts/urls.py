from django.urls import path
from rest_framework_simplejwt.views import TokenVerifyView

from . import views

app_name = "accounts"

urlpatterns = [
    path("register", views.RegisterView.as_view(), name="register"),
    path("token", views.LoginView.as_view(), name="token"),
    path("token/refresh", views.RefreshView.as_view(), name="token-refresh"),
    path("token/verify", TokenVerifyView.as_view(), name="token-verify"),
    path("logout", views.LogoutView.as_view(), name="logout"),
    path("me", views.MeView.as_view(), name="me"),
    path("password/change", views.ChangePasswordView.as_view(), name="password-change"),
]
