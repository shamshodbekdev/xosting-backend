import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from fakeredis import aioredis as fake_aioredis
from fastapi.testclient import TestClient

from app.config import Settings
from app.ispmanager import ISPManagerClient
from app.main import create_app

JWT_SECRET = "test-jwt-secret-with-at-least-32-bytes!!"


class FakeISPManager:
    """In-memory stand-in for the ISPmanager HTTP API."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.users: dict[str, dict] = {}
        self.fail_with: dict | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append(params)
        if self.fail_with:
            return httpx.Response(200, json={"doc": {"error": self.fail_with}})
        func = params["func"]
        if func == "user.edit" and "name" in params:
            if params["name"] in self.users:
                return httpx.Response(200, json={"doc": {"error": {"$type": "exists"}}})
            self.users[params["name"]] = {"active": True}
        elif func == "user.suspend":
            self.users[params["elid"]]["active"] = False
        elif func == "user.resume":
            self.users[params["elid"]]["active"] = True
        elif func == "user.delete":
            self.users.pop(params["elid"], None)
        return httpx.Response(200, content=json.dumps({"doc": {"ok": {}}}))

    def funcs(self) -> list[str]:
        return [c["func"] for c in self.calls]


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
def ispmanager() -> FakeISPManager:
    return FakeISPManager()


@pytest.fixture
def redis_client():
    return fake_aioredis.FakeRedis()


@pytest.fixture
def client(tmp_path, ispmanager, redis_client) -> Iterator[TestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        db_auto_create=True,
        jwt_secret=JWT_SECRET,
        ispmanager_url="https://panel.test:1500",
        ispmanager_username="root",
        ispmanager_password="secret",
    )
    isp_client = ISPManagerClient(
        settings.ispmanager_url, "root", "secret", transport=httpx.MockTransport(ispmanager.handler)
    )
    app = create_app(settings, redis_client=redis_client, ispmanager=isp_client)
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
