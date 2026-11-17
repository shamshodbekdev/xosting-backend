"""domain-service: domain registration requests, availability and DNS zones."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from sqlalchemy import text
from xosting_shared.fastapi_kit import health_router, install_error_handlers, install_observability
from xosting_shared.logger import configure_logging

from .config import Settings, get_settings
from .db import Base, create_engine, create_sessionmaker
from .rdap import RDAPClient
from .routers import domains


async def _check_database(request: Request) -> None:
    async with request.app.state.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _check_redis(request: Request) -> None:
    await request.app.state.redis.ping()


def create_app(
    settings: Settings | None = None,
    *,
    redis_client: aioredis.Redis | None = None,
    rdap: RDAPClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.service_name, settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        if settings.db_auto_create:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.redis = redis_client or aioredis.from_url(settings.redis_url)
        app.state.rdap = rdap or RDAPClient(
            settings.rdap_base_url, timeout=settings.rdap_timeout_seconds
        )
        try:
            yield
        finally:
            await app.state.rdap.aclose()
            await app.state.redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="xosting.net Domain Service",
        version="1.0.0",
        description="Domain availability, registration requests and DNS zone management.",
        lifespan=lifespan,
        docs_url="/api/v1/domains/docs",
        openapi_url="/api/v1/domains/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings
    install_observability(app, settings.service_name)
    install_error_handlers(app)
    app.include_router(health_router({"database": _check_database, "redis": _check_redis}))
    app.include_router(domains.router)
    return app
