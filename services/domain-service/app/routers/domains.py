import logging
import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from xosting_shared.security import Principal
from xosting_shared.security.fastapi import get_current_principal, require_staff

from ..db import get_session, utcnow
from ..events import emit
from ..models import DnsRecord, Domain, DomainStatus, RecordType
from ..rdap import Availability
from ..schemas import (
    AvailabilityOut,
    DomainCreate,
    DomainOut,
    DomainUpdate,
    RecordIn,
    RecordOut,
    TldPrice,
)
from ..validation import InvalidDomain, normalise_domain, tld_of

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/domains", tags=["domains"])

Session = Annotated[AsyncSession, Depends(get_session)]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
Staff = Annotated[Principal, Depends(require_staff)]


def _parse_domain(request: Request, raw: str) -> tuple[str, str]:
    try:
        name = normalise_domain(raw)
    except InvalidDomain as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    tld = tld_of(name)
    if tld not in request.app.state.settings.tld_prices:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f".{tld} is not supported.")
    return name, tld


async def _check_availability(request: Request, session: AsyncSession, name: str) -> Availability:
    if await session.scalar(select(Domain.id).where(Domain.name == name)):
        return Availability.TAKEN

    cache_key = f"domain-service:availability:{name}"
    redis = request.app.state.redis
    try:
        cached = await redis.get(cache_key)
    except RedisError:
        logger.warning("availability cache unavailable", exc_info=True)
        cached = None
    if cached:
        return Availability(cached.decode())

    result = await request.app.state.rdap.lookup(name)
    if result != Availability.UNKNOWN:
        try:
            await redis.set(
                cache_key, result.value, ex=request.app.state.settings.availability_cache_seconds
            )
        except RedisError:
            logger.warning("availability cache unavailable", exc_info=True)
    return result


async def _get_domain(session: AsyncSession, domain_id: uuid.UUID, principal: Principal) -> Domain:
    domain = await session.get(Domain, domain_id)
    if domain is None or (not principal.is_staff and str(domain.user_id) != principal.user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Domain not found.")
    return domain


async def _ensure_no_cname_conflict(
    session: AsyncSession,
    domain_id: uuid.UUID,
    payload: RecordIn,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """RFC 1034 §3.6.2: a CNAME cannot coexist with any other record at the same name."""
    query = select(DnsRecord.type).where(
        DnsRecord.domain_id == domain_id, DnsRecord.name == payload.name
    )
    if exclude_id is not None:
        query = query.where(DnsRecord.id != exclude_id)
    existing = set(await session.scalars(query))
    if (payload.type == RecordType.CNAME and existing) or RecordType.CNAME in existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A CNAME cannot coexist with other records of the same name."
        )


# ---- public -------------------------------------------------------------------
@router.get("/tlds", response_model=list[TldPrice])
async def list_tlds(request: Request) -> list[TldPrice]:
    prices = request.app.state.settings.tld_prices
    return [TldPrice(tld=tld, price_per_year=price) for tld, price in sorted(prices.items())]


@router.get("/availability", response_model=AvailabilityOut)
async def check_availability(
    request: Request, session: Session, name: Annotated[str, Query(min_length=3, max_length=253)]
) -> AvailabilityOut:
    domain, tld = _parse_domain(request, name)
    result = await _check_availability(request, session, domain)
    return AvailabilityOut(
        domain=domain,
        status=result,
        price_per_year=request.app.state.settings.tld_prices[tld]
        if result != Availability.TAKEN
        else None,
    )


# ---- domains ------------------------------------------------------------------
@router.get("", response_model=list[DomainOut])
async def list_domains(
    session: Session,
    principal: CurrentPrincipal,
    user_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DomainOut]:
    query = select(Domain).order_by(Domain.created_at.desc()).limit(limit).offset(offset)
    if not principal.is_staff:
        query = query.where(Domain.user_id == uuid.UUID(principal.user_id))
    elif user_id:
        query = query.where(Domain.user_id == user_id)
    return [DomainOut.model_validate(d) for d in await session.scalars(query)]


@router.post("", response_model=DomainOut, status_code=status.HTTP_201_CREATED)
async def request_registration(
    payload: DomainCreate, request: Request, session: Session, principal: CurrentPrincipal
) -> DomainOut:
    """Record a registration request. The registrar integration (or staff)
    completes it via ``POST /{id}/activate`` once payment clears."""
    name, tld = _parse_domain(request, payload.name)
    if await _check_availability(request, session, name) == Availability.TAKEN:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{name} is already registered.")

    settings = request.app.state.settings
    domain = Domain(
        user_id=uuid.UUID(principal.user_id),
        name=name,
        tld=tld,
        years=payload.years,
        auto_renew=payload.auto_renew,
        nameservers=payload.nameservers or list(settings.default_nameservers),
    )
    session.add(domain)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"{name} is already registered.") from exc

    await emit(
        request,
        "domain.registration_requested",
        user_id=principal.user_id,
        payload={
            "domain_id": str(domain.id),
            "domain": name,
            "years": domain.years,
            "amount": str(settings.tld_prices[tld] * domain.years),
            "currency": "UZS",
        },
    )
    return DomainOut.model_validate(domain)


@router.get("/{domain_id}", response_model=DomainOut)
async def get_domain(
    domain_id: uuid.UUID, session: Session, principal: CurrentPrincipal
) -> DomainOut:
    return DomainOut.model_validate(await _get_domain(session, domain_id, principal))


@router.patch("/{domain_id}", response_model=DomainOut)
async def update_domain(
    domain_id: uuid.UUID, payload: DomainUpdate, session: Session, principal: CurrentPrincipal
) -> DomainOut:
    domain = await _get_domain(session, domain_id, principal)
    for field, value in payload.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(domain, field, value)
    await session.commit()
    return DomainOut.model_validate(domain)


@router.post("/{domain_id}/activate", response_model=DomainOut)
async def activate_domain(
    domain_id: uuid.UUID, request: Request, session: Session, staff: Staff
) -> DomainOut:
    domain = await _get_domain(session, domain_id, staff)
    if domain.status != DomainStatus.PENDING_REGISTRATION:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Domain is {domain.status}.")
    now = utcnow()
    domain.status = DomainStatus.ACTIVE
    domain.registered_at = now
    domain.expires_at = now + timedelta(days=365 * domain.years)
    await session.commit()
    await emit(
        request,
        "domain.registered",
        user_id=str(domain.user_id),
        payload={
            "domain_id": str(domain.id),
            "domain": domain.name,
            "expires_at": domain.expires_at.isoformat(),
        },
    )
    return DomainOut.model_validate(domain)


# ---- DNS records ----------------------------------------------------------------
@router.get("/{domain_id}/records", response_model=list[RecordOut])
async def list_records(
    domain_id: uuid.UUID, session: Session, principal: CurrentPrincipal
) -> list[RecordOut]:
    await _get_domain(session, domain_id, principal)
    rows = await session.scalars(
        select(DnsRecord)
        .where(DnsRecord.domain_id == domain_id)
        .order_by(DnsRecord.type, DnsRecord.name)
    )
    return [RecordOut.model_validate(r) for r in rows]


@router.post("/{domain_id}/records", response_model=RecordOut, status_code=status.HTTP_201_CREATED)
async def create_record(
    domain_id: uuid.UUID, payload: RecordIn, session: Session, principal: CurrentPrincipal
) -> RecordOut:
    await _get_domain(session, domain_id, principal)
    await _ensure_no_cname_conflict(session, domain_id, payload)
    record = DnsRecord(domain_id=domain_id, **payload.model_dump())
    session.add(record)
    await session.commit()
    return RecordOut.model_validate(record)


@router.put("/{domain_id}/records/{record_id}", response_model=RecordOut)
async def update_record(
    domain_id: uuid.UUID,
    record_id: uuid.UUID,
    payload: RecordIn,
    session: Session,
    principal: CurrentPrincipal,
) -> RecordOut:
    await _get_domain(session, domain_id, principal)
    record = await session.get(DnsRecord, record_id)
    if record is None or record.domain_id != domain_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record not found.")
    await _ensure_no_cname_conflict(session, domain_id, payload, exclude_id=record_id)
    for field, value in payload.model_dump().items():
        setattr(record, field, value)
    await session.commit()
    return RecordOut.model_validate(record)


@router.delete("/{domain_id}/records/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_record(
    domain_id: uuid.UUID, record_id: uuid.UUID, session: Session, principal: CurrentPrincipal
) -> Response:
    await _get_domain(session, domain_id, principal)
    record = await session.get(DnsRecord, record_id)
    if record is None or record.domain_id != domain_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record not found.")
    await session.delete(record)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
