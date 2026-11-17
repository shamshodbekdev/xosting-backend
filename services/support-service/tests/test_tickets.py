import time

from xosting_shared.events import DEFAULT_STREAM

from app.routers.ws import ACTIVE_CONNECTIONS

from .conftest import make_token

URL = "/api/v1/support/tickets"


def _open_ticket(client, headers, subject="Site is down"):
    return client.post(
        URL,
        json={"subject": subject, "department": "technical", "priority": "high", "body": "Help!"},
        headers=headers,
    )


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


async def _event_types(redis_client) -> list[str]:
    return [f[b"type"].decode() for _, f in await redis_client.xrange(DEFAULT_STREAM)]


async def test_ticket_conversation_updates_status(
    client, customer_headers, staff_headers, redis_client
):
    ticket = _open_ticket(client, customer_headers).json()
    assert ticket["status"] == "awaiting_staff"
    assert [m["body"] for m in ticket["messages"]] == ["Help!"]

    reply = client.post(
        f"{URL}/{ticket['id']}/messages", json={"body": "Looking into it"}, headers=staff_headers
    )
    assert reply.status_code == 201
    assert reply.json()["author_role"] == "staff"

    detail = client.get(f"{URL}/{ticket['id']}", headers=customer_headers).json()
    assert detail["status"] == "awaiting_customer"
    assert len(detail["messages"]) == 2
    assert await _event_types(redis_client) == ["support.ticket.created", "support.ticket.replied"]


def test_tickets_are_private_but_visible_to_staff(client, customer_headers, staff_headers):
    ticket_id = _open_ticket(client, customer_headers).json()["id"]
    other = {"Authorization": f"Bearer {make_token('33333333-3333-3333-3333-333333333333')}"}

    assert client.get(f"{URL}/{ticket_id}", headers=other).status_code == 404
    assert client.get(URL, headers=other).json() == []
    assert len(client.get(URL, headers=staff_headers).json()) == 1


def test_customers_can_only_close_or_resolve(client, customer_headers, staff_headers):
    ticket_id = _open_ticket(client, customer_headers).json()["id"]

    assert (
        client.patch(
            f"{URL}/{ticket_id}", json={"priority": "urgent"}, headers=customer_headers
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"{URL}/{ticket_id}", json={"priority": "urgent"}, headers=staff_headers
        ).status_code
        == 200
    )
    closed = client.patch(f"{URL}/{ticket_id}", json={"status": "closed"}, headers=customer_headers)
    assert closed.json()["status"] == "closed"
    assert (
        client.post(
            f"{URL}/{ticket_id}/messages", json={"body": "hi"}, headers=customer_headers
        ).status_code
        == 409
    )


def test_websocket_rejects_bad_token_and_foreign_ticket(client, customer_headers):
    ticket_id = _open_ticket(client, customer_headers).json()["id"]

    with client.websocket_connect(f"/ws/support/tickets/{ticket_id}?access_token=garbage") as ws:
        message = ws.receive()
        assert message["type"] == "websocket.close"
        assert message["code"] == 4401

    stranger = make_token("44444444-4444-4444-4444-444444444444")
    with client.websocket_connect(f"/ws/support/tickets/{ticket_id}?access_token={stranger}") as ws:
        assert ws.receive()["code"] == 4404


def test_websocket_receives_messages_posted_via_rest_and_ws(
    client, customer_id, customer_headers, staff_headers
):
    ticket_id = _open_ticket(client, customer_headers).json()["id"]
    token = make_token(customer_id)

    with client.websocket_connect(f"/ws/support/tickets/{ticket_id}?access_token={token}") as ws:
        assert ws.receive_json() == {"type": "connected", "ticket_id": ticket_id}
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}

        # A staff reply via REST (could be served by another replica) is pushed to the socket.
        client.post(
            f"{URL}/{ticket_id}/messages", json={"body": "We are on it"}, headers=staff_headers
        )
        pushed = ws.receive_json()
        assert pushed["type"] == "message"
        assert pushed["message"]["body"] == "We are on it"
        assert pushed["status"] == "awaiting_customer"

        # Messages sent over the socket are persisted and echoed through Pub/Sub.
        ws.send_json({"type": "message", "body": "Thanks!"})
        echoed = ws.receive_json()
        assert echoed["message"]["author_role"] == "customer"

        ws.send_json({"type": "bogus"})
        assert ws.receive_json()["type"] == "error"

        # Close from the client and let the server finish its cleanup before the
        # TestClient tears down its event loop.
        ws.close(1000)
        _wait_for(lambda: ACTIVE_CONNECTIONS._value.get() == 0)

    bodies = [
        m["body"]
        for m in client.get(f"{URL}/{ticket_id}", headers=customer_headers).json()["messages"]
    ]
    assert bodies == ["Help!", "We are on it", "Thanks!"]
