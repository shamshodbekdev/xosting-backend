"""Shared pytest fixtures and helpers (loaded as a plugin by the root conftest)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import fakeredis
import jwt
import pytest
from django.conf import settings
from rest_framework.test import APIClient

from apps.billing.models import Plan


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    client = fakeredis.FakeRedis()
    monkeypatch.setattr("apps.core.events.get_redis", lambda: client)
    monkeypatch.setattr("apps.core.views.get_redis", lambda: client)
    return client


def make_token(user_id: str, role: str = "customer") -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": user_id,
        "token_type": "access",
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "iss": settings.SIMPLE_JWT["ISSUER"],
        "aud": settings.SIMPLE_JWT["AUDIENCE"],
        "role": role,
        "is_staff": role in ("staff", "admin"),
        "email": f"{user_id[:8]}@example.com",
    }
    return jwt.encode(claims, settings.SIMPLE_JWT["SIGNING_KEY"], algorithm="HS256")


@pytest.fixture
def customer_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def customer_client(customer_id) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(customer_id)}")
    return client


@pytest.fixture
def staff_client() -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(str(uuid.uuid4()), 'staff')}")
    return client


@pytest.fixture
def plan(db) -> Plan:
    return Plan.objects.create(
        code="hosting-start",
        name="Start",
        service_type="hosting",
        price=Decimal("29000.00"),
        period_months=1,
    )


def event_types(redis_client) -> list[str]:
    return [fields[b"type"].decode() for _, fields in redis_client.xrange(settings.EVENTS_STREAM)]
