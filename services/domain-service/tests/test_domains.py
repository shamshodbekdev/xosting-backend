import pytest

from app.validation import InvalidDomain, normalise_domain, validate_record

from .conftest import make_token

URL = "/api/v1/domains"


def test_tld_price_list_is_public(client):
    response = client.get(f"{URL}/tlds")
    assert response.status_code == 200
    assert {"tld": "uz", "price_per_year": "60000", "currency": "UZS"} in response.json()


@pytest.mark.parametrize(
    ("name", "expected"),
    [("Mening-Saytim.UZ", "available"), ("google.com", "taken"), ("timeout.com", "unknown")],
)
def test_availability(client, name, expected):
    response = client.get(f"{URL}/availability", params={"name": name})
    assert response.status_code == 200
    assert response.json()["status"] == expected


def test_availability_is_cached(client, rdap_calls):
    for _ in range(3):
        client.get(f"{URL}/availability", params={"name": "cached.uz"})
    assert rdap_calls == ["cached.uz"]


def test_unsupported_tld_and_invalid_names(client):
    assert client.get(f"{URL}/availability", params={"name": "example.xyz"}).status_code == 422
    assert client.get(f"{URL}/availability", params={"name": "-bad-.uz"}).status_code == 422


def test_registration_flow(client, customer_headers, staff_headers):
    created = client.post(
        URL, json={"name": "xosting-demo.uz", "years": 2}, headers=customer_headers
    )
    assert created.status_code == 201, created.text
    domain = created.json()
    assert domain["status"] == "pending_registration"
    assert domain["nameservers"] == ["ns1.xosting.net", "ns2.xosting.net"]

    assert (
        client.post(URL, json={"name": "xosting-demo.uz"}, headers=staff_headers).status_code == 409
    )
    assert client.post(URL, json={"name": "taken.uz"}, headers=customer_headers).status_code == 409

    assert (
        client.post(f"{URL}/{domain['id']}/activate", headers=customer_headers).status_code == 403
    )
    activated = client.post(f"{URL}/{domain['id']}/activate", headers=staff_headers).json()
    assert activated["status"] == "active"
    assert activated["expires_at"] is not None


def test_domains_are_private(client, customer_headers):
    domain_id = client.post(URL, json={"name": "private.uz"}, headers=customer_headers).json()["id"]
    other = {"Authorization": f"Bearer {make_token('22222222-2222-2222-2222-222222222222')}"}

    assert client.get(f"{URL}/{domain_id}", headers=other).status_code == 404
    assert client.get(URL, headers=other).json() == []


def test_update_nameservers_validation(client, customer_headers):
    domain_id = client.post(URL, json={"name": "ns-test.uz"}, headers=customer_headers).json()["id"]

    bad = client.patch(
        f"{URL}/{domain_id}", json={"nameservers": ["only-one.example"]}, headers=customer_headers
    )
    good = client.patch(
        f"{URL}/{domain_id}",
        json={"nameservers": ["NS1.Cloudflare.com", "ns2.cloudflare.com."], "auto_renew": False},
        headers=customer_headers,
    )

    assert bad.status_code == 422
    assert good.json()["nameservers"] == ["ns1.cloudflare.com", "ns2.cloudflare.com"]
    assert good.json()["auto_renew"] is False


def test_dns_record_crud_and_cname_rules(client, customer_headers):
    domain_id = client.post(URL, json={"name": "dns-test.uz"}, headers=customer_headers).json()[
        "id"
    ]
    records = f"{URL}/{domain_id}/records"

    a = client.post(
        records,
        json={"type": "A", "name": "www", "content": "203.0.113.10"},
        headers=customer_headers,
    )
    assert a.status_code == 201

    cname_clash = client.post(
        records,
        json={"type": "CNAME", "name": "www", "content": "dns-test.uz"},
        headers=customer_headers,
    )
    assert cname_clash.status_code == 409

    bad_ip = client.post(
        records, json={"type": "A", "name": "@", "content": "999.1.1.1"}, headers=customer_headers
    )
    no_priority = client.post(
        records, json={"type": "MX", "content": "mx.dns-test.uz"}, headers=customer_headers
    )
    assert bad_ip.status_code == 422
    assert no_priority.status_code == 422

    updated = client.put(
        f"{records}/{a.json()['id']}",
        json={"type": "A", "name": "www", "content": "203.0.113.20", "ttl": 300},
        headers=customer_headers,
    )
    assert updated.json()["content"] == "203.0.113.20"
    assert client.delete(f"{records}/{a.json()['id']}", headers=customer_headers).status_code == 204
    assert client.get(records, headers=customer_headers).json() == []


def test_validation_helpers():
    assert normalise_domain("Тест.uz") == "xn--e1aybc.uz"
    with pytest.raises(InvalidDomain):
        normalise_domain("localhost")
    assert validate_record("CAA", "@", '0 issue "letsencrypt.org"', None)
    with pytest.raises(ValueError, match="apex"):
        validate_record("CNAME", "@", "example.com", None)


def test_availability_survives_cache_outage(client, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    async def broken(*_args, **_kwargs):
        raise RedisConnectionError("redis down")

    redis = client.app.state.redis
    monkeypatch.setattr(redis, "get", broken)
    monkeypatch.setattr(redis, "set", broken)

    response = client.get(f"{URL}/availability", params={"name": "no-cache.uz"})
    assert response.status_code == 200
    assert response.json()["status"] == "available"
