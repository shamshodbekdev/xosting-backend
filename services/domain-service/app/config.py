from decimal import Decimal
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True, env_ignore_empty=True)

    service_name: str = "domain-service"
    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = Field(..., description="postgresql+asyncpg://user:pass@host/db")
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_auto_create: bool = False

    redis_url: str = "redis://localhost:6379/0"
    events_stream: str = "xosting.events"

    jwt_secret: str
    jwt_issuer: str = "xosting-auth"
    jwt_audience: str = "xosting-api"

    rdap_base_url: str = "https://rdap.org"
    rdap_timeout_seconds: float = 5.0
    availability_cache_seconds: int = 300

    default_nameservers: list[str] = ["ns1.xosting.net", "ns2.xosting.net"]
    # Supported TLDs and yearly registration price (UZS).
    tld_prices: dict[str, Decimal] = {
        "uz": Decimal("60000"),
        "com": Decimal("165000"),
        "net": Decimal("185000"),
        "org": Decimal("175000"),
        "io": Decimal("520000"),
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
