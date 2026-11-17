from functools import lru_cache

import redis
from django.conf import settings


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2, health_check_interval=30
    )
