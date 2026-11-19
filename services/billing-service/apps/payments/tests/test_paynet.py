import pytest
from defusedxml import ElementTree

from apps.billing.models import Invoice
from apps.payments.models import PaymentProvider

pytestmark = pytest.mark.django_db

WEBHOOK = "/api/v1/payments/webhook/paynet/"


@pytest.fixture
def paynet(enable_provider):
    return enable_provider("paynet", username="paynet", password="pw", service_id="77")


def envelope(method: str, body: str, username="paynet", password="pw") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:ns1="http://uws.provider.com/">
  <soapenv:Body>
    <ns1:{method}Arguments>
      <password>{password}</password>
      <username>{username}</username>
      <serviceId>77</serviceId>
      {body}
    </ns1:{method}Arguments>
  </soapenv:Body>
</soapenv:Envelope>"""


def account(number: str) -> str:
    return f"<parameters><paramKey>account</paramKey><paramValue>{number}</paramValue></parameters>"


def soap(client, method, body="", **auth) -> dict:
    response = client.post(WEBHOOK, data=envelope(method, body, **auth), content_type="text/xml")
    assert response.status_code == 200, response.content
    root = ElementTree.fromstring(response.content)
    result = root[0][0]
    assert result.tag == f"{{http://uws.provider.com/}}{method}Result"
    fields: dict = {}
    for child in result:
        if len(child):
            fields.setdefault(child.tag, []).append({c.tag: c.text for c in child})
        else:
            fields[child.tag] = child.text
    fields["status"] = int(fields["status"])
    return fields


def perform(client, invoice, transaction_id="pn-1", amount="2900000"):
    return soap(
        client,
        "PerformTransaction",
        f"<amount>{amount}</amount><transactionId>{transaction_id}</transactionId>"
        f"<transactionTime>2026-11-19T10:00:00+05:00</transactionTime>{account(invoice.number)}",
    )


def test_get_information(client, invoice, paynet):
    result = soap(client, "GetInformation", account(invoice.number))
    assert result["status"] == 0
    params = {p["paramKey"]: p["paramValue"] for p in result["parameters"]}
    assert params == {"invoice": invoice.number, "amount": "2900000", "currency": "UZS"}
    assert soap(client, "GetInformation", account("INV-x"))["status"] == 302


def test_perform_check_and_cancel(client, invoice, paynet):
    result = perform(client, invoice)
    assert result["status"] == 0 and result["providerTrnId"]
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID
    assert invoice.payments.get().provider == "paynet"

    assert perform(client, invoice)["status"] == 201  # duplicate transactionId

    checked = soap(client, "CheckTransaction", "<transactionId>pn-1</transactionId>")
    assert (checked["status"], checked["transactionState"]) == (0, "1")

    cancelled = soap(client, "CancelTransaction", "<transactionId>pn-1</transactionId>")
    assert cancelled["status"] == 77


def test_wrong_amount_and_unknown_transaction(client, invoice, paynet):
    assert perform(client, invoice, amount="100")["status"] == 413
    unknown = soap(client, "CheckTransaction", "<transactionId>nope</transactionId>")
    assert unknown["status"] == 203


def test_statement(client, invoice, paynet):
    perform(client, invoice)
    result = soap(
        client,
        "GetStatement",
        "<dateFrom>2000-01-01T00:00:00+05:00</dateFrom><dateTo>2100-01-01T00:00:00+05:00</dateTo>",
    )
    [row] = result["statements"]
    assert row["transactionId"] == "pn-1" and row["amount"] == "2900000"


def test_authentication(client, invoice, paynet):
    assert soap(client, "GetInformation", account(invoice.number), password="x")["status"] == 412


def test_malformed_xml_is_soap_fault(client, paynet):
    response = client.post(WEBHOOK, data="<not-xml", content_type="text/xml")
    assert response.status_code == 400
    assert b"Fault" in response.content


def test_entity_expansion_is_rejected(client, paynet):
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
        "<soapenv:Envelope xmlns:soapenv='http://schemas.xmlsoap.org/soap/envelope/'>"
        "<soapenv:Body>&a;</soapenv:Body></soapenv:Envelope>"
    )
    assert client.post(WEBHOOK, data=bomb, content_type="text/xml").status_code == 400


def test_disabled_provider_is_403(client, invoice, paynet):
    PaymentProvider.objects.filter(key="paynet").update(is_active=False)
    response = client.post(
        WEBHOOK, data=envelope("GetInformation", account(invoice.number)), content_type="text/xml"
    )
    assert response.status_code == 403
