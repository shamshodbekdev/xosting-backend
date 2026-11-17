import pytest
from django.urls import reverse
from rest_framework.throttling import SimpleRateThrottle

pytestmark = pytest.mark.django_db

# DRF binds throttle rates at import time, so patch the class attribute directly.
RATES = {"login": "2/min", "register": "2/min", "password": "2/min"}


class _BrokenCache:
    def get(self, *_args, **_kwargs):
        raise ConnectionError("redis is down")

    def set(self, *_args, **_kwargs):
        raise ConnectionError("redis is down")


def _login(api_client):
    return api_client.post(reverse("accounts:token"), {"email": "a@b.com", "password": "nope"})


def test_login_is_throttled(api_client, monkeypatch):
    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES", RATES)

    assert [_login(api_client).status_code for _ in range(3)] == [401, 401, 429]


def test_throttle_fails_open_when_cache_is_down(api_client, monkeypatch):
    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES", RATES)
    monkeypatch.setattr(SimpleRateThrottle, "cache", _BrokenCache())

    assert [_login(api_client).status_code for _ in range(3)] == [401, 401, 401]
