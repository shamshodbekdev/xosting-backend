import fakeredis
import pytest

from apps.core import redis as redis_module


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    client = fakeredis.FakeRedis()
    redis_module.get_redis.cache_clear()
    monkeypatch.setattr(redis_module, "get_redis", lambda: client)
    monkeypatch.setattr("apps.core.events.get_redis", lambda: client)
    monkeypatch.setattr("apps.core.views.get_redis", lambda: client)
    return client


@pytest.fixture(autouse=True)
def _isolate_throttle_counters():
    """Throttle counters live in the cache: reset them so tests are independent."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(email="ali@example.com", password="Sup3r-Secret-Pass")
