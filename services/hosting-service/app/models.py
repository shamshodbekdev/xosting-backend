import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, TimestampMixin


class AccountStatus(enum.StrEnum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    FAILED = "failed"
    TERMINATED = "terminated"


class HostingAccount(TimestampMixin, Base):
    __tablename__ = "hosting_accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True, nullable=True)
    plan_code: Mapped[str] = mapped_column(String(64))
    domain: Mapped[str] = mapped_column(String(253), unique=True)
    panel_username: Mapped[str] = mapped_column(String(32), unique=True)
    server: Mapped[str] = mapped_column(String(253))
    status: Mapped[AccountStatus] = mapped_column(
        Enum(
            AccountStatus,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=AccountStatus.PENDING,
        index=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
