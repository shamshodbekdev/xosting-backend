import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import AccountStatus

_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)(?!-)([a-z0-9-]{1,63}(?<!-)\.)+[a-z]{2,63}$")


class AccountCreate(BaseModel):
    user_id: uuid.UUID
    plan_code: str = Field(min_length=1, max_length=64)
    domain: str = Field(examples=["example.uz"])
    subscription_id: uuid.UUID | None = None

    @field_validator("domain")
    @classmethod
    def normalise_domain(cls, value: str) -> str:
        value = value.strip().lower().removeprefix("www.")
        value = value.encode("idna").decode("ascii")
        if not _DOMAIN_RE.match(value):
            raise ValueError("Invalid domain name")
        return value


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    subscription_id: uuid.UUID | None
    plan_code: str
    domain: str
    panel_username: str
    server: str
    status: AccountStatus
    last_error: str | None
    provisioned_at: datetime | None
    suspended_at: datetime | None
    created_at: datetime


class AccountCreated(BaseModel):
    account: AccountOut
    initial_password: str = Field(
        description="Control-panel password. Returned exactly once and never stored."
    )
    panel_url: str


class PasswordReset(BaseModel):
    password: str
    panel_url: str


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int
