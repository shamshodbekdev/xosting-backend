"""Real-time ticket conversation over WebSocket.

    ws(s)://<host>/ws/support/tickets/{ticket_id}?access_token=<JWT>

Browsers cannot set ``Authorization`` headers on WebSocket handshakes, so the
access token travels in the query string (the gateway strips it from logs).

Every replica subscribes to the Redis Pub/Sub channel of the ticket, so a
message posted on replica A reaches clients connected to replica B.

Client -> server frames::

    {"type": "message", "body": "..."}   persist + broadcast
    {"type": "typing"}                   broadcast only (ephemeral)
    {"type": "ping"}                     server answers {"type": "pong"}

Server -> client frames::

    {"type": "connected", "ticket_id": "..."}   subscription is live
    {"type": "message", "message": {...}, "status": "awaiting_staff"}
    {"type": "ticket", "ticket": {...}}
    {"type": "typing", "author_id": "...", "author_role": "staff"}
    {"type": "error", "detail": "..."}

Close codes: 4401 unauthenticated, 4404 ticket not found / not yours.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from prometheus_client import Gauge
from pydantic import ValidationError
from redis.asyncio.client import PubSub
from xosting_shared.security import Principal, TokenError
from xosting_shared.security.fastapi import authenticate_token

from ..schemas import MessageIn
from ..service import (
    TicketAccessDenied,
    TicketClosed,
    broadcast,
    channel_for,
    load_ticket,
    post_message,
)

logger = logging.getLogger(__name__)
router = APIRouter()

ACTIVE_CONNECTIONS = Gauge(
    "support_websocket_connections_active",
    "Open ticket WebSocket connections on this replica.",
    multiprocess_mode="livesum",
)

CLOSE_UNAUTHENTICATED = 4401
CLOSE_NOT_FOUND = 4404


async def _relay_pubsub(websocket: WebSocket, pubsub: PubSub) -> None:
    while True:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is None:
            continue
        data = message["data"]
        await websocket.send_text(data.decode() if isinstance(data, bytes) else data)


async def _handle_client(websocket: WebSocket, ticket_id: uuid.UUID, principal: Principal) -> None:
    state = websocket.app.state
    while True:
        try:
            frame = await asyncio.wait_for(
                websocket.receive_json(), timeout=state.settings.ws_heartbeat_seconds * 2
            )
        except TimeoutError:
            await websocket.close(code=1001, reason="idle timeout")
            return
        except (json.JSONDecodeError, KeyError):
            await websocket.send_json({"type": "error", "detail": "Frames must be JSON objects."})
            continue

        kind = frame.get("type") if isinstance(frame, dict) else None
        if kind == "ping":
            await websocket.send_json({"type": "pong"})
        elif kind == "typing":
            await broadcast(
                state,
                ticket_id,
                {
                    "type": "typing",
                    "author_id": principal.user_id,
                    "author_role": "staff" if principal.is_staff else "customer",
                },
            )
        elif kind == "message":
            try:
                body = MessageIn(body=frame.get("body", "")).body
            except ValidationError:
                await websocket.send_json({"type": "error", "detail": "Message body is invalid."})
                continue
            async with state.sessionmaker() as session:
                try:
                    ticket = await load_ticket(session, ticket_id, principal)
                    await post_message(state, session, ticket, principal, body)
                except TicketClosed:
                    await websocket.send_json({"type": "error", "detail": "Ticket is closed."})
        else:
            await websocket.send_json({"type": "error", "detail": f"Unknown frame type: {kind!r}"})


@router.websocket("/ws/support/tickets/{ticket_id}")
async def ticket_socket(websocket: WebSocket, ticket_id: uuid.UUID) -> None:
    await websocket.accept()
    state = websocket.app.state

    token = websocket.query_params.get("access_token", "")
    try:
        principal = authenticate_token(token, state.settings)
    except TokenError:
        await websocket.close(code=CLOSE_UNAUTHENTICATED, reason="authentication failed")
        return

    async with state.sessionmaker() as session:
        try:
            await load_ticket(session, ticket_id, principal)
        except TicketAccessDenied:
            await websocket.close(code=CLOSE_NOT_FOUND, reason="ticket not found")
            return

    # Subscribe *before* announcing readiness so no message can slip through
    # between the handshake and the subscription.
    pubsub = state.redis.pubsub()
    await pubsub.subscribe(channel_for(state, ticket_id))
    await websocket.send_json({"type": "connected", "ticket_id": str(ticket_id)})

    ACTIVE_CONNECTIONS.inc()
    relay = asyncio.create_task(_relay_pubsub(websocket, pubsub))
    client = asyncio.create_task(_handle_client(websocket, ticket_id, principal))
    logger.info(
        "websocket connected", extra={"ticket_id": str(ticket_id), "user_id": principal.user_id}
    )
    try:
        done, pending = await asyncio.wait({relay, client}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                logger.error("websocket task failed", exc_info=exc)
    finally:
        try:
            for task in (relay, client):
                task.cancel()
            await asyncio.gather(relay, client, return_exceptions=True)
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.aclose()
        finally:
            ACTIVE_CONNECTIONS.dec()
        logger.info("websocket disconnected", extra={"ticket_id": str(ticket_id)})
