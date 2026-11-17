import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from xosting_shared.security import Principal
from xosting_shared.security.fastapi import get_current_principal

from ..db import get_session
from ..events import emit
from ..models import Ticket, TicketMessage, TicketStatus
from ..schemas import MessageIn, MessageOut, TicketCreate, TicketDetail, TicketOut, TicketUpdate
from ..service import TicketAccessDenied, TicketClosed, load_ticket, post_message, ticket_updated

router = APIRouter(prefix="/api/v1/support/tickets", tags=["support tickets"])

Session = Annotated[AsyncSession, Depends(get_session)]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]

CUSTOMER_SETTABLE_STATUSES = {TicketStatus.RESOLVED, TicketStatus.CLOSED}


async def _ticket_or_404(
    session: AsyncSession, ticket_id: uuid.UUID, principal: Principal
) -> Ticket:
    try:
        return await load_ticket(session, ticket_id, principal)
    except TicketAccessDenied as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found.") from exc


@router.post("", response_model=TicketDetail, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    payload: TicketCreate, request: Request, session: Session, principal: CurrentPrincipal
) -> TicketDetail:
    ticket = Ticket(
        user_id=uuid.UUID(principal.user_id),
        subject=payload.subject,
        department=payload.department,
        priority=payload.priority,
    )
    ticket.messages.append(
        TicketMessage(author_id=ticket.user_id, author_role="customer", body=payload.body)
    )
    session.add(ticket)
    await session.commit()
    await emit(
        request,
        "support.ticket.created",
        user_id=principal.user_id,
        payload={
            "ticket_id": str(ticket.id),
            "subject": ticket.subject,
            "department": ticket.department.value,
            "priority": ticket.priority.value,
        },
    )
    return TicketDetail.model_validate(ticket)


@router.get("", response_model=list[TicketOut])
async def list_tickets(
    session: Session,
    principal: CurrentPrincipal,
    ticket_status: Annotated[TicketStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TicketOut]:
    query = select(Ticket).order_by(Ticket.updated_at.desc()).limit(limit).offset(offset)
    if not principal.is_staff:
        query = query.where(Ticket.user_id == uuid.UUID(principal.user_id))
    if ticket_status:
        query = query.where(Ticket.status == ticket_status)
    return [TicketOut.model_validate(t) for t in await session.scalars(query)]


@router.get("/{ticket_id}", response_model=TicketDetail)
async def get_ticket(
    ticket_id: uuid.UUID, session: Session, principal: CurrentPrincipal
) -> TicketDetail:
    await _ticket_or_404(session, ticket_id, principal)
    ticket = await session.scalar(
        select(Ticket).where(Ticket.id == ticket_id).options(selectinload(Ticket.messages))
    )
    return TicketDetail.model_validate(ticket)


@router.post(
    "/{ticket_id}/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED
)
async def add_message(
    ticket_id: uuid.UUID,
    payload: MessageIn,
    request: Request,
    session: Session,
    principal: CurrentPrincipal,
) -> MessageOut:
    ticket = await _ticket_or_404(session, ticket_id, principal)
    try:
        message = await post_message(request.app.state, session, ticket, principal, payload.body)
    except TicketClosed as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ticket is closed.") from exc
    return MessageOut.model_validate(message)


@router.patch("/{ticket_id}", response_model=TicketOut)
async def update_ticket(
    ticket_id: uuid.UUID,
    payload: TicketUpdate,
    request: Request,
    session: Session,
    principal: CurrentPrincipal,
) -> TicketOut:
    ticket = await _ticket_or_404(session, ticket_id, principal)
    changes = payload.model_dump(exclude_unset=True)
    customer_allowed = (
        set(changes) <= {"status"} and changes.get("status") in CUSTOMER_SETTABLE_STATUSES
    )
    if not principal.is_staff and not customer_allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Customers may only resolve or close their tickets."
        )
    for field, value in changes.items():
        setattr(ticket, field, value)
    await session.commit()
    await ticket_updated(request.app.state, ticket)
    return TicketOut.model_validate(ticket)
