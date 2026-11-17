import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, TimestampMixin


def _values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


class DomainStatus(enum.StrEnum):
    PENDING_REGISTRATION = "pending_registration"
    ACTIVE = "active"
    EXPIRED = "expired"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class RecordType(enum.StrEnum):
    A = "A"
    AAAA = "AAAA"
    CNAME = "CNAME"
    MX = "MX"
    TXT = "TXT"
    NS = "NS"
    SRV = "SRV"
    CAA = "CAA"


class Domain(TimestampMixin, Base):
    __tablename__ = "domains"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    name: Mapped[str] = mapped_column(String(253), unique=True)
    tld: Mapped[str] = mapped_column(String(63), index=True)
    status: Mapped[DomainStatus] = mapped_column(
        Enum(DomainStatus, native_enum=False, length=32, values_callable=_values),
        default=DomainStatus.PENDING_REGISTRATION,
    )
    years: Mapped[int] = mapped_column(Integer, default=1)
    auto_renew: Mapped[bool] = mapped_column(default=True)
    nameservers: Mapped[list[str]] = mapped_column(JSON, default=list)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    records: Mapped[list["DnsRecord"]] = relationship(
        back_populates="domain", cascade="all, delete-orphan", order_by="DnsRecord.created_at"
    )


class DnsRecord(TimestampMixin, Base):
    __tablename__ = "dns_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    domain_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[RecordType] = mapped_column(
        Enum(RecordType, native_enum=False, length=8, values_callable=_values)
    )
    name: Mapped[str] = mapped_column(String(253))
    content: Mapped[str] = mapped_column(String(4096))
    ttl: Mapped[int] = mapped_column(Integer, default=3600)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)

    domain: Mapped[Domain] = relationship(back_populates="records")
