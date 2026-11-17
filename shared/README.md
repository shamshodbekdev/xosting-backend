# shared

Cross-cutting libraries used by every xosting.net service. This directory is both:

- a **Python package** `xosting_shared` (see `pyproject.toml`), installed into each
  Python image with `pip install ./shared` (or `"./shared[fastapi]"` for the FastAPI services), and
- a **Go module** `github.com/shamshodbekdev/xosting-backend/shared`, consumed by the Go services
  through a `replace` directive and the root `go.work`.

| Package | Python (`xosting_shared.*`) | Go |
|---|---|---|
| `logger/` | `configure_logging`, `django_logging_config`, `JsonFormatter`, request-context middleware for Django (`logger.django`) and ASGI (`logger.asgi`) | `logger.New`, `WithRequestID`, `RequestID` (slog JSON handler with trace correlation) |
| `metrics/` | RED metrics + `/metrics` for Django (`metrics.django`) and ASGI (`metrics.asgi`); gunicorn multiprocess support | `metrics.Middleware`, `metrics.Handler`, hijack-safe `StatusRecorder` |
| `tracing/` | `instrument_django`, `instrument_fastapi` (enabled only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set) | `tracing.Setup`, `tracing.Middleware`, `tracing.Transport` |
| `security/` | `decode_access_token`, `Principal`; FastAPI dependencies in `security.fastapi` | — |
| `events/` | `Event`, `EventPublisher`, `AsyncEventPublisher` (Redis Streams) | — |
| `fastapi_kit/` | `install_observability`, `install_error_handlers`, `health_router` | — |

Every service uses the same log fields, metric names and error envelope, whichever language it is written in.
