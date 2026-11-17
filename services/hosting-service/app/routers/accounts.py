import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from xosting_shared.security import Principal
from xosting_shared.security.fastapi import get_current_principal, require_staff

from ..db import get_session, utcnow
from ..events import emit
from ..ispmanager import ISPManagerError
from ..models import AccountStatus, HostingAccount
from ..provisioning import generate_password, generate_username, provision_account
from ..schemas import AccountCreate, AccountCreated, AccountOut, Page, PasswordReset

router = APIRouter(prefix="/api/v1/hosting/accounts", tags=["hosting accounts"])

Session = Annotated[AsyncSession, Depends(get_session)]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
Staff = Annotated[Principal, Depends(require_staff)]


async def _get_account(
    session: AsyncSession, account_id: uuid.UUID, principal: Principal
) -> HostingAccount:
    account = await session.get(HostingAccount, account_id)
    if account is None or (not principal.is_staff and str(account.user_id) != principal.user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hosting account not found.")
    return account


@router.get("", response_model=Page[AccountOut])
async def list_accounts(
    session: Session,
    principal: CurrentPrincipal,
    user_id: uuid.UUID | None = None,
    account_status: Annotated[AccountStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AccountOut]:
    query = select(HostingAccount)
    if not principal.is_staff:
        query = query.where(HostingAccount.user_id == uuid.UUID(principal.user_id))
    elif user_id:
        query = query.where(HostingAccount.user_id == user_id)
    if account_status:
        query = query.where(HostingAccount.status == account_status)

    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(
        query.order_by(HostingAccount.created_at.desc()).limit(limit).offset(offset)
    )
    return Page(
        items=[AccountOut.model_validate(r) for r in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=AccountCreated, status_code=status.HTTP_202_ACCEPTED)
async def create_account(
    payload: AccountCreate,
    request: Request,
    background: BackgroundTasks,
    session: Session,
    _staff: Staff,
) -> AccountCreated:
    """Register a hosting account and provision it asynchronously on ISPmanager.

    Responds with ``202 Accepted``; poll the account until ``status`` is
    ``active`` (or ``failed``). The panel password is returned exactly once.
    """
    settings = request.app.state.settings
    if payload.plan_code not in settings.hosting_plan_presets:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown hosting plan.")

    account = HostingAccount(
        user_id=payload.user_id,
        subscription_id=payload.subscription_id,
        plan_code=payload.plan_code,
        domain=payload.domain,
        panel_username=generate_username(payload.domain),
        server=settings.hosting_server_name,
    )
    session.add(account)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Domain or subscription already has a hosting account."
        ) from exc

    password = generate_password()
    background.add_task(provision_account, request.app.state, account.id, password)
    return AccountCreated(
        account=AccountOut.model_validate(account),
        initial_password=password,
        panel_url=request.app.state.ispmanager.panel_url,
    )


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: uuid.UUID, session: Session, principal: CurrentPrincipal
) -> AccountOut:
    return AccountOut.model_validate(await _get_account(session, account_id, principal))


@router.post("/{account_id}/password", response_model=PasswordReset)
async def reset_password(
    account_id: uuid.UUID, request: Request, session: Session, principal: CurrentPrincipal
) -> PasswordReset:
    account = await _get_account(session, account_id, principal)
    if account.status != AccountStatus.ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, "Account is not active.")
    password = generate_password()
    client = request.app.state.ispmanager
    try:
        await client.set_password(account.panel_username, password)
    except ISPManagerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Control panel is unavailable.") from exc
    await emit(
        request,
        "hosting.password_reset",
        user_id=str(account.user_id),
        payload={"account_id": str(account.id), "domain": account.domain},
    )
    return PasswordReset(password=password, panel_url=client.panel_url)


async def _transition(
    request: Request,
    session: AsyncSession,
    account: HostingAccount,
    *,
    allowed_from: set[AccountStatus],
    target: AccountStatus,
    action: str,
) -> HostingAccount:
    if account.status not in allowed_from:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot {action} a {account.status} account."
        )
    client = request.app.state.ispmanager
    operation = {
        AccountStatus.SUSPENDED: client.suspend_user,
        AccountStatus.ACTIVE: client.resume_user,
        AccountStatus.TERMINATED: client.delete_user,
    }[target]
    try:
        await operation(account.panel_username)
    except ISPManagerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Control panel error: {exc}") from exc

    account.status = target
    account.suspended_at = utcnow() if target == AccountStatus.SUSPENDED else None
    await session.commit()
    await emit(
        request,
        f"hosting.account.{target.value}",
        user_id=str(account.user_id),
        payload={"account_id": str(account.id), "domain": account.domain},
    )
    return account


@router.post("/{account_id}/suspend", response_model=AccountOut)
async def suspend_account(
    account_id: uuid.UUID, request: Request, session: Session, staff: Staff
) -> AccountOut:
    account = await _get_account(session, account_id, staff)
    return AccountOut.model_validate(
        await _transition(
            request,
            session,
            account,
            allowed_from={AccountStatus.ACTIVE},
            target=AccountStatus.SUSPENDED,
            action="suspend",
        )
    )


@router.post("/{account_id}/resume", response_model=AccountOut)
async def resume_account(
    account_id: uuid.UUID, request: Request, session: Session, staff: Staff
) -> AccountOut:
    account = await _get_account(session, account_id, staff)
    return AccountOut.model_validate(
        await _transition(
            request,
            session,
            account,
            allowed_from={AccountStatus.SUSPENDED},
            target=AccountStatus.ACTIVE,
            action="resume",
        )
    )


@router.delete("/{account_id}", response_model=AccountOut)
async def terminate_account(
    account_id: uuid.UUID, request: Request, session: Session, staff: Staff
) -> AccountOut:
    account = await _get_account(session, account_id, staff)
    return AccountOut.model_validate(
        await _transition(
            request,
            session,
            account,
            allowed_from={AccountStatus.ACTIVE, AccountStatus.SUSPENDED, AccountStatus.FAILED},
            target=AccountStatus.TERMINATED,
            action="terminate",
        )
    )
