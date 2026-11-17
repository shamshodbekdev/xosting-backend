"""Domain-event publishing over Redis Streams.

All services append events to a single stream (``xosting.events`` by
default). Consumers such as ``notification-service`` read it with consumer
groups, which gives at-least-once delivery, replay and horizontal scaling.

Envelope (all values are strings, as required by Redis Streams)::

    id           UUID of the event (idempotency key for consumers)
    type         e.g. "user.registered", "invoice.paid"
    source       emitting service
    occurred_at  RFC 3339 timestamp (UTC)
    user_id      subject user, if any
    payload      JSON-encoded event body
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_STREAM", "Event", "EventPublisher", "AsyncEventPublisher"]

DEFAULT_STREAM = "xosting.events"
DEFAULT_MAXLEN = 100_000


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    user_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="milliseconds")
    )

    def to_fields(self) -> dict[str, str]:
        return {
            "id": self.id,
            "type": self.type,
            "source": self.source,
            "occurred_at": self.occurred_at,
            "user_id": self.user_id or "",
            "payload": json.dumps(self.payload, default=str, ensure_ascii=False),
        }


class EventPublisher:
    """Synchronous publisher (Django / Celery)."""

    def __init__(
        self, redis_client: Any, stream: str = DEFAULT_STREAM, maxlen: int = DEFAULT_MAXLEN
    ):
        self._redis = redis_client
        self._stream = stream
        self._maxlen = maxlen

    def publish(self, event: Event) -> str | None:
        try:
            entry_id = self._redis.xadd(
                self._stream, event.to_fields(), maxlen=self._maxlen, approximate=True
            )
        except Exception:  # broker outages must never break the business transaction
            logger.exception("failed to publish event", extra={"event_type": event.type})
            return None
        logger.info("event published", extra={"event_type": event.type, "event_id": event.id})
        return entry_id.decode() if isinstance(entry_id, bytes) else entry_id


class AsyncEventPublisher:
    """Asyncio publisher (FastAPI services)."""

    def __init__(
        self, redis_client: Any, stream: str = DEFAULT_STREAM, maxlen: int = DEFAULT_MAXLEN
    ):
        self._redis = redis_client
        self._stream = stream
        self._maxlen = maxlen

    async def publish(self, event: Event) -> str | None:
        try:
            entry_id = await self._redis.xadd(
                self._stream, event.to_fields(), maxlen=self._maxlen, approximate=True
            )
        except Exception:
            logger.exception("failed to publish event", extra={"event_type": event.type})
            return None
        logger.info("event published", extra={"event_type": event.type, "event_id": event.id})
        return entry_id.decode() if isinstance(entry_id, bytes) else entry_id
