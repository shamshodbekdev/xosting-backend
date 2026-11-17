import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from fakeredis import aioredis as fake_aioredis
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.rdap import RDAPClient

JWT_SECRET = "test-jwt-secret-with-at-least-32-bytes!!"
REGISTERED = {"google.com", "taken.uz"}


def make_token(user_id: str, role: str = "customer") -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user_id,
            "token_type": "access",
            "role": role,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "iss": "xosting-auth",
            "aud": "xosting-api",
        },
        JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def rdap_calls() -> list[str]:
    return []


@pytest.fixture
def client(tmp_path, rdap_calls) -> Iterator[TestClient]:
    def rdap_handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        rdap_calls.append(name)
        if name == "timeout.com":
            raise httpx.ConnectTimeout("boom")
        return httpx.Response(200 if name in REGISTERED else 404, json={})

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        db_auto_create=True,
        jwt_secret=JWT_SECRET,
    )
    app = create_app(
        settings,
        redis_client=fake_aioredis.FakeRedis(),
        rdap=RDAPClient("https://rdap.test", transport=httpx.MockTransport(rdap_handler)),
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def customer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(str(uuid.uuid4()))}"}


@pytest.fixture
def staff_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(str(uuid.uuid4()), 'staff')}"}
