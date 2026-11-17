import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fakeredis import aioredis as fake_aioredis
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

JWT_SECRET = "test-jwt-secret-with-at-least-32-bytes!!"


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
def redis_client():
    return fake_aioredis.FakeRedis()


@pytest.fixture
def client(tmp_path, redis_client) -> Iterator[TestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        db_auto_create=True,
        jwt_secret=JWT_SECRET,
    )
    app = create_app(settings, redis_client=redis_client)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def customer_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def customer_headers(customer_id) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(customer_id)}"}


@pytest.fixture
def staff_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(str(uuid.uuid4()), 'staff')}"}
