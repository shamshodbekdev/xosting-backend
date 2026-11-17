import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .models import Department, Priority, TicketStatus


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    author_id: uuid.UUID
    author_role: str
    body: str
    created_at: datetime


class TicketCreate(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    department: Department = Department.TECHNICAL
    priority: Priority = Priority.NORMAL
    body: str = Field(min_length=1, max_length=10_000)


class TicketUpdate(BaseModel):
    status: TicketStatus | None = None
    priority: Priority | None = None
    assignee_id: uuid.UUID | None = None


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    subject: str
    department: Department
    priority: Priority
    status: TicketStatus
    assignee_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class TicketDetail(TicketOut):
    messages: list[MessageOut]
