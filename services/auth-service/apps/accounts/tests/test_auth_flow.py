import jwt
import pytest
from django.conf import settings
from django.urls import reverse

from apps.accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "Sup3r-Secret-Pass"


def _login(api_client, email="ali@example.com", password=PASSWORD):
    return api_client.post(reverse("accounts:token"), {"email": email, "password": password})


def test_register_creates_user_returns_tokens_and_publishes_event(api_client, fake_redis):
    response = api_client.post(
        reverse("accounts:register"),
        {"email": "New.User@Example.com", "password": PASSWORD, "first_name": "Nodir"},
        format="json",
    )

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["user"]["email"] == "new.user@example.com"
    assert {"access", "refresh"} <= body["tokens"].keys()
    assert User.objects.filter(email="new.user@example.com").exists()

    entries = fake_redis.xrange(settings.EVENTS_STREAM)
    assert len(entries) == 1
    assert entries[0][1][b"type"] == b"user.registered"


def test_register_rejects_duplicate_email_and_weak_password(api_client, user):
    duplicate = api_client.post(
        reverse("accounts:register"), {"email": user.email, "password": PASSWORD}, format="json"
    )
    weak = api_client.post(
        reverse("accounts:register"), {"email": "x@example.com", "password": "123"}, format="json"
    )

    assert duplicate.status_code == 400
    assert "email" in duplicate.json()["error"]["details"]
    assert weak.status_code == 400
    assert weak.json()["error"]["code"] == "invalid"


def test_access_token_carries_identity_claims(api_client, user):
    response = _login(api_client)

    assert response.status_code == 200
    claims = jwt.decode(
        response.json()["access"],
        settings.SIMPLE_JWT["SIGNING_KEY"],
        algorithms=["HS256"],
        audience=settings.SIMPLE_JWT["AUDIENCE"],
        issuer=settings.SIMPLE_JWT["ISSUER"],
    )
    assert claims["sub"] == str(user.id)
    assert claims["email"] == user.email
    assert claims["role"] == "customer"
    assert claims["token_type"] == "access"


def test_me_requires_auth_and_allows_profile_update(api_client, user):
    assert api_client.get(reverse("accounts:me")).status_code == 401

    access = _login(api_client).json()["access"]
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    response = api_client.patch(
        reverse("accounts:me"), {"telegram_chat_id": "123456", "role": "admin"}, format="json"
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.telegram_chat_id == "123456"
    assert user.role == "customer"  # role is read-only


def test_logout_blacklists_refresh_token(api_client, user):
    tokens = _login(api_client).json()
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    assert (
        api_client.post(reverse("accounts:logout"), {"refresh": tokens["refresh"]}).status_code
        == 205
    )
    refreshed = api_client.post(reverse("accounts:token-refresh"), {"refresh": tokens["refresh"]})
    assert refreshed.status_code == 401


def test_change_password_revokes_existing_sessions(api_client, user):
    tokens = _login(api_client).json()
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    response = api_client.post(
        reverse("accounts:password-change"),
        {"current_password": PASSWORD, "new_password": "An0ther-Str0ng-Pass"},
    )

    assert response.status_code == 204
    assert (
        api_client.post(
            reverse("accounts:token-refresh"), {"refresh": tokens["refresh"]}
        ).status_code
        == 401
    )
    assert _login(api_client, password="An0ther-Str0ng-Pass").status_code == 200


def test_internal_contact_endpoint_requires_internal_token(api_client, user):
    url = reverse("internal:user-contact", kwargs={"id": user.id})

    assert api_client.get(url).status_code == 403
    response = api_client.get(url, HTTP_X_INTERNAL_TOKEN=settings.INTERNAL_API_TOKEN)
    assert response.status_code == 200
    assert response.json()["email"] == user.email


def test_probes(api_client, db):
    assert api_client.get("/healthz").json() == {"status": "ok"}
    ready = api_client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": "ok", "redis": "ok"}
    metrics = api_client.get("/metrics")
    assert metrics.status_code == 200
    assert b"http_requests_total" in metrics.content
