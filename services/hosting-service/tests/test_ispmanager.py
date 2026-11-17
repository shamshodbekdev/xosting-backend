import httpx
import pytest

from app.ispmanager import ISPManagerClient, ISPManagerError, ISPManagerUnavailable


def _client(handler) -> ISPManagerClient:
    return ISPManagerClient(
        "https://panel.test:1500", "root", "pw", transport=httpx.MockTransport(handler)
    )


async def test_error_document_is_raised_with_code():
    client = _client(
        lambda _r: httpx.Response(
            200,
            json={
                "doc": {
                    "error": {"$type": "exists", "$object": "user", "msg": {"$": "User exists"}}
                }
            },
        )
    )
    with pytest.raises(ISPManagerError) as info:
        await client.create_user("u1", "pw", "start")
    assert info.value.already_exists
    assert str(info.value) == "User exists"


async def test_server_errors_are_retryable():
    client = _client(lambda _r: httpx.Response(503))
    with pytest.raises(ISPManagerUnavailable):
        await client.suspend_user("u1")


async def test_list_users_flattens_values():
    client = _client(
        lambda _r: httpx.Response(
            200, json={"doc": {"elem": [{"name": {"$": "u1"}, "active": {"$": "on"}}]}}
        )
    )
    assert await client.list_users() == [{"name": "u1", "active": "on"}]
