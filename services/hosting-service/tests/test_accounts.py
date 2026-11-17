from xosting_shared.events import DEFAULT_STREAM

from .conftest import make_token

URL = "/api/v1/hosting/accounts"


def _create(client, staff_headers, customer_id, domain="Example.UZ"):
    return client.post(
        URL,
        json={"user_id": customer_id, "plan_code": "hosting-start", "domain": domain},
        headers=staff_headers,
    )


async def _event_types(redis_client) -> list[str]:
    return [f[b"type"].decode() for _, f in await redis_client.xrange(DEFAULT_STREAM)]


async def test_staff_creates_account_which_is_provisioned(
    client, staff_headers, customer_id, customer_headers, ispmanager, redis_client
):
    response = _create(client, staff_headers, customer_id)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["account"]["domain"] == "example.uz"
    assert len(body["initial_password"]) == 20
    assert ispmanager.funcs() == ["user.edit", "webdomain.edit"]
    # The password travels in the POST body, never in the URL.
    assert all("authinfo" in call for call in ispmanager.calls)

    account = client.get(f"{URL}/{body['account']['id']}", headers=customer_headers).json()
    assert account["status"] == "active"
    assert account["provisioned_at"] is not None
    assert await _event_types(redis_client) == ["hosting.account.provisioned"]


def test_customers_cannot_create_accounts_or_see_others(
    client, staff_headers, customer_id, customer_headers
):
    assert _create(client, customer_headers, customer_id).status_code == 403

    account_id = _create(client, staff_headers, customer_id).json()["account"]["id"]
    stranger = {"Authorization": f"Bearer {make_token('11111111-1111-1111-1111-111111111111')}"}
    assert client.get(f"{URL}/{account_id}", headers=stranger).status_code == 404
    assert client.get(URL, headers=stranger).json()["total"] == 0
    assert client.get(URL, headers=customer_headers).json()["total"] == 1


def test_duplicate_domain_conflicts(client, staff_headers, customer_id):
    assert _create(client, staff_headers, customer_id).status_code == 202
    response = _create(client, staff_headers, customer_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_invalid_domain_and_plan_are_rejected(client, staff_headers, customer_id):
    bad_domain = _create(client, staff_headers, customer_id, domain="not a domain")
    bad_plan = client.post(
        URL,
        json={"user_id": customer_id, "plan_code": "nope", "domain": "ok.uz"},
        headers=staff_headers,
    )

    assert bad_domain.status_code == 422
    assert bad_domain.json()["error"]["code"] == "validation_error"
    assert bad_plan.status_code == 422


async def test_panel_error_marks_account_failed(
    client, staff_headers, customer_id, ispmanager, redis_client
):
    ispmanager.fail_with = {"$type": "access", "msg": {"$": "Access denied"}}

    account_id = _create(client, staff_headers, customer_id).json()["account"]["id"]

    account = client.get(f"{URL}/{account_id}", headers=staff_headers).json()
    assert account["status"] == "failed"
    assert account["last_error"] == "Access denied"
    assert await _event_types(redis_client) == ["hosting.account.failed"]


def test_suspend_resume_terminate_lifecycle(
    client, staff_headers, customer_id, customer_headers, ispmanager
):
    account_id = _create(client, staff_headers, customer_id).json()["account"]["id"]

    assert client.post(f"{URL}/{account_id}/suspend", headers=customer_headers).status_code == 403
    suspended = client.post(f"{URL}/{account_id}/suspend", headers=staff_headers)
    assert suspended.json()["status"] == "suspended"
    assert client.post(f"{URL}/{account_id}/suspend", headers=staff_headers).status_code == 409
    assert (
        client.post(f"{URL}/{account_id}/resume", headers=staff_headers).json()["status"]
        == "active"
    )
    assert (
        client.delete(f"{URL}/{account_id}", headers=staff_headers).json()["status"] == "terminated"
    )
    assert ispmanager.users == {}


def test_owner_can_reset_panel_password(
    client, staff_headers, customer_id, customer_headers, ispmanager
):
    account_id = _create(client, staff_headers, customer_id).json()["account"]["id"]

    response = client.post(f"{URL}/{account_id}/password", headers=customer_headers)

    assert response.status_code == 200
    assert ispmanager.calls[-1]["elid"].startswith("uexample")
    assert ispmanager.calls[-1]["passwd"] == response.json()["password"]


def test_requests_without_token_are_rejected(client):
    response = client.get(URL)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_probes_and_metrics(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json()["checks"] == {"database": "ok", "redis": "ok"}
    assert "http_requests_total" in client.get("/metrics").text
