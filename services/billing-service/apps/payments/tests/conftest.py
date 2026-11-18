import uuid

import pytest

from apps.billing import services as billing
from apps.billing.models import Invoice
from apps.payments.models import PaymentProvider


@pytest.fixture
def invoice(plan, customer_id) -> Invoice:
    _, invoice = billing.subscribe(customer_id, plan)
    return invoice


@pytest.fixture
def other_invoice(plan) -> Invoice:
    _, invoice = billing.subscribe(str(uuid.uuid4()), plan)
    return invoice


@pytest.fixture
def enable_provider(db):
    def _enable(key: str, **config) -> PaymentProvider:
        record = PaymentProvider.objects.get(key=key)
        record.is_active = True
        record.config = config
        record.save()
        return record

    return _enable
