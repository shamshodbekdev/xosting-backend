from typing import Any

from fastapi import Request
from xosting_shared.events import AsyncEventPublisher, Event


async def emit(
    request_or_state: Any, event_type: str, *, user_id: str | None, payload: dict
) -> None:
    state = (
        request_or_state.app.state if isinstance(request_or_state, Request) else request_or_state
    )
    publisher = AsyncEventPublisher(state.redis, stream=state.settings.events_stream)
    await publisher.publish(
        Event(type=event_type, source=state.settings.service_name, user_id=user_id, payload=payload)
    )
