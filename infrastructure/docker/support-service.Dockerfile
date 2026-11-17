# syntax=docker/dockerfile:1.7
# support-service (FastAPI + WebSocket + Redis Pub/Sub) — build context: repository root.
#   docker build -f infrastructure/docker/support-service.Dockerfile .                # runtime image
#   docker build -f infrastructure/docker/support-service.Dockerfile --target test .  # lint + tests

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app

FROM base AS builder
RUN python -m venv $VIRTUAL_ENV
COPY services/support-service/requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install -r /tmp/requirements.txt
COPY shared /tmp/shared
RUN --mount=type=cache,target=/root/.cache/pip pip install "/tmp/shared[fastapi]"

FROM builder AS test
COPY services/support-service/requirements-dev.txt /tmp/requirements-dev.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install -r /tmp/requirements-dev.txt
COPY services/support-service/ /app/
RUN ruff check . && ruff format --check . && pytest -q -p no:cacheprovider

FROM base AS runtime
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app services/support-service/ /app/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
# Migrations: alembic upgrade head
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header", "--timeout-graceful-shutdown", "20"]
