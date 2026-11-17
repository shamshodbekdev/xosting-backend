"""Ticket operations shared by the REST API and the WebSocket gateway."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from xosting_shared.security import Principal

from .events import emit
from .models import Ticket, TicketMessage, TicketStatus
from .schemas import MessageOut, TicketOut

logger = logging.getLogger(__name__)


class TicketAccessDenied(Exception):
    pass


class TicketClosed(Exception):
    pass


def channel_for(state: Any, ticket_id: uuid.UUID | str) -> str:
    return f"{state.settings.pubsub_channel_prefix}{ticket_id}"


async def load_ticket(session: AsyncSession, ticket_id: uuid.UUID, principal: Principal) -> Ticket:
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None or (not principal.is_staff and str(ticket.user_id) != principal.user_id):
        raise TicketAccessDenied
    return ticket


async def broadcast(state: Any, ticket_id: uuid.UUID | str, payload: dict) -> None:
    """Fan out to every WebSocket subscriber, on every replica, via Redis Pub/Sub."""
    await state.redis.publish(channel_for(state, ticket_id), json.dumps(payload, default=str))


async def post_message(
    state: Any, session: AsyncSession, ticket: Ticket, principal: Principal, body: str
) -> TicketMessage:
    if ticket.status == TicketStatus.CLOSED:
        raise TicketClosed

    message = TicketMessage(
        ticket_id=ticket.id,
        author_id=uuid.UUID(principal.user_id),
        author_role="staff" if principal.is_staff else "customer",
        body=body,
    )
    session.add(message)
    ticket.status = (
        TicketStatus.AWAITING_CUSTOMER if principal.is_staff else TicketStatus.AWAITING_STAFF
    )
    await session.commit()
    await session.refresh(ticket)

    out = MessageOut.model_validate(message).model_dump(mode="json")
    await broadcast(
        state, ticket.id, {"type": "message", "message": out, "status": ticket.status.value}
    )
    if principal.is_staff:
        await emit(
            state,
            "support.ticket.replied",
            user_id=str(ticket.user_id),
            payload={"ticket_id": str(ticket.id), "subject": ticket.subject, "preview": body[:200]},
        )
    return message


async def ticket_updated(state: Any, ticket: Ticket) -> None:
    await broadcast(
        state,
        ticket.id,
        {"type": "ticket", "ticket": TicketOut.model_validate(ticket).model_dump(mode="json")},
    )
