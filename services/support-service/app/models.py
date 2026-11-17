import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, TimestampMixin


def _values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


class Department(enum.StrEnum):
    TECHNICAL = "technical"
    BILLING = "billing"
    SALES = "sales"
    ABUSE = "abuse"


class Priority(enum.StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TicketStatus(enum.StrEnum):
    AWAITING_STAFF = "awaiting_staff"
    AWAITING_CUSTOMER = "awaiting_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"


class Ticket(TimestampMixin, Base):
    __tablename__ = "tickets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    subject: Mapped[str] = mapped_column(String(200))
    department: Mapped[Department] = mapped_column(
        Enum(Department, native_enum=False, length=16, values_callable=_values)
    )
    priority: Mapped[Priority] = mapped_column(
        Enum(Priority, native_enum=False, length=16, values_callable=_values),
        default=Priority.NORMAL,
    )
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, native_enum=False, length=24, values_callable=_values),
        default=TicketStatus.AWAITING_STAFF,
        index=True,
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)

    messages: Mapped[list["TicketMessage"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="TicketMessage.created_at"
    )


class TicketMessage(TimestampMixin, Base):
    __tablename__ = "ticket_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    author_role: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)

    ticket: Mapped[Ticket] = relationship(back_populates="messages")
