from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True, env_ignore_empty=True)

    service_name: str = "support-service"
    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = Field(..., description="postgresql+asyncpg://user:pass@host/db")
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_auto_create: bool = False

    redis_url: str = "redis://localhost:6379/0"
    events_stream: str = "xosting.events"
    pubsub_channel_prefix: str = "support:ticket:"

    jwt_secret: str
    jwt_issuer: str = "xosting-auth"
    jwt_audience: str = "xosting-api"

    ws_heartbeat_seconds: float = 25.0
    max_message_length: int = 10_000


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
