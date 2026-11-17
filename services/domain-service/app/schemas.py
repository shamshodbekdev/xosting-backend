import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import DomainStatus, RecordType
from .rdap import Availability
from .validation import is_hostname, validate_record


class TldPrice(BaseModel):
    tld: str
    price_per_year: Decimal
    currency: str = "UZS"


class AvailabilityOut(BaseModel):
    domain: str
    status: Availability
    price_per_year: Decimal | None


def _check_nameservers(value: list[str] | None) -> list[str] | None:
    if value is None:
        return value
    cleaned = [ns.strip().lower().rstrip(".") for ns in value]
    if not 2 <= len(cleaned) <= 13:
        raise ValueError("Provide between 2 and 13 nameservers.")
    if len(set(cleaned)) != len(cleaned) or not all(is_hostname(ns) for ns in cleaned):
        raise ValueError("Nameservers must be unique, valid hostnames.")
    return cleaned


class DomainCreate(BaseModel):
    name: str = Field(examples=["mening-saytim.uz"])
    years: int = Field(1, ge=1, le=10)
    auto_renew: bool = True
    nameservers: list[str] | None = None

    _ns = field_validator("nameservers")(_check_nameservers)


class DomainUpdate(BaseModel):
    auto_renew: bool | None = None
    nameservers: list[str] | None = None

    _ns = field_validator("nameservers")(_check_nameservers)


class DomainOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    tld: str
    status: DomainStatus
    years: int
    auto_renew: bool
    nameservers: list[str]
    registered_at: datetime | None
    expires_at: datetime | None
    created_at: datetime


class RecordIn(BaseModel):
    type: RecordType
    name: str = Field("@", max_length=253)
    content: str = Field(min_length=1, max_length=4096)
    ttl: int = Field(3600, ge=60, le=86400)
    priority: int | None = Field(None, ge=0, le=65535)

    @model_validator(mode="after")
    def _validate(self) -> "RecordIn":
        self.name = self.name.strip().lower()
        self.content = validate_record(self.type.value, self.name, self.content, self.priority)
        return self


class RecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: RecordType
    name: str
    content: str
    ttl: int
    priority: int | None
