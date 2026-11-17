"""Account provisioning workflow (runs after the HTTP response is sent)."""

from __future__ import annotations

import logging
import re
import secrets
import string
import uuid
from typing import Any

from .db import utcnow
from .events import emit
from .ispmanager import ISPManagerError
from .models import AccountStatus, HostingAccount

logger = logging.getLogger(__name__)

_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#%^*_-+="


def generate_password(length: int = 20) -> str:
    """Strong random password containing every character class."""
    while True:
        password = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(not c.isalnum() for c in password)
        ):
            return password


def generate_username(domain: str) -> str:
    """ISPmanager-safe username: ``u`` + up to 10 domain chars + 4 random hex chars."""
    stem = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())[:10] or "site"
    return f"u{stem}{secrets.token_hex(2)}"


async def provision_account(state: Any, account_id: uuid.UUID, password: str) -> None:
    """Create the panel user and web domain, then flip the account to ``active``.

    Idempotent: "already exists" answers from ISPmanager are treated as
    success, so a retried provisioning run converges to the same state.
    """
    client = state.ispmanager
    settings = state.settings

    async with state.sessionmaker() as session:
        account = await session.get(HostingAccount, account_id)
        if account is None or account.status not in (AccountStatus.PENDING, AccountStatus.FAILED):
            return
        account.status = AccountStatus.PROVISIONING
        await session.commit()

        preset = settings.hosting_plan_presets[account.plan_code]
        try:
            try:
                await client.create_user(account.panel_username, password, preset)
            except ISPManagerError as exc:
                if not exc.already_exists:
                    raise
            try:
                await client.create_web_domain(
                    account.domain,
                    owner=account.panel_username,
                    email=f"webmaster@{account.domain}",
                )
            except ISPManagerError as exc:
                if not exc.already_exists:
                    raise
        except ISPManagerError as exc:
            account.status = AccountStatus.FAILED
            account.last_error = str(exc)
            await session.commit()
            logger.error(
                "provisioning failed", extra={"account_id": str(account_id), "error": str(exc)}
            )
            await emit(
                state,
                "hosting.account.failed",
                user_id=str(account.user_id),
                payload={"account_id": str(account.id), "domain": account.domain},
            )
            return

        account.status = AccountStatus.ACTIVE
        account.last_error = None
        account.provisioned_at = utcnow()
        await session.commit()
        logger.info("account provisioned", extra={"account_id": str(account_id)})
        await emit(
            state,
            "hosting.account.provisioned",
            user_id=str(account.user_id),
            payload={
                "account_id": str(account.id),
                "domain": account.domain,
                "panel_username": account.panel_username,
                "panel_url": client.panel_url,
                "server": account.server,
            },
        )
