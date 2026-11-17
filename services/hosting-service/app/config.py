from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True, env_ignore_empty=True)

    service_name: str = "hosting-service"
    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = Field(..., description="postgresql+asyncpg://user:pass@host/db")
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_auto_create: bool = Field(False, description="Create tables on startup (tests/dev only).")

    redis_url: str = "redis://localhost:6379/0"
    events_stream: str = "xosting.events"

    jwt_secret: str
    jwt_issuer: str = "xosting-auth"
    jwt_audience: str = "xosting-api"

    ispmanager_url: str = Field(..., description="e.g. https://panel.xosting.net:1500")
    ispmanager_username: str
    ispmanager_password: SecretStr
    ispmanager_verify_tls: bool = True
    ispmanager_timeout_seconds: float = 20.0
    hosting_server_name: str = "web-01.xosting.net"

    # plan code (billing-service) -> ISPmanager preset (template)
    hosting_plan_presets: dict[str, str] = {
        "hosting-start": "start",
        "hosting-business": "business",
        "hosting-pro": "pro",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
