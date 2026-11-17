"""Publish domain events *after* the surrounding DB transaction commits.

Publishing inside ``transaction.on_commit`` guarantees consumers never see an
event for data that was rolled back.
"""

from typing import Any

from django.conf import settings
from django.db import transaction
from xosting_shared.events import Event, EventPublisher

from .redis import get_redis


def publish_event(event_type: str, *, user_id: str | None, payload: dict[str, Any]) -> Event:
    event = Event(
        type=event_type,
        source=settings.SERVICE_NAME,
        user_id=user_id,
        payload=payload,
    )
    publisher = EventPublisher(get_redis(), stream=settings.EVENTS_STREAM)
    transaction.on_commit(lambda: publisher.publish(event))
    return event
