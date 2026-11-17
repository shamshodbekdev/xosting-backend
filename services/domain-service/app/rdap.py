"""Registry availability lookups via RDAP (RFC 9082/9083).

RDAP is the IETF successor of WHOIS with structured JSON and well-defined
HTTP semantics: ``404`` means the name is not registered.
"""

from __future__ import annotations

import enum
import logging

import httpx

logger = logging.getLogger(__name__)


class Availability(enum.StrEnum):
    AVAILABLE = "available"
    TAKEN = "taken"
    UNKNOWN = "unknown"


class RDAPClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={"Accept": "application/rdap+json", "User-Agent": "xosting-domain-service/1.0"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def lookup(self, domain: str) -> Availability:
        try:
            response = await self._http.get(f"/domain/{domain}")
        except httpx.HTTPError as exc:
            logger.warning("rdap lookup failed", extra={"domain": domain, "error": str(exc)})
            return Availability.UNKNOWN
        if response.status_code == 404:
            return Availability.AVAILABLE
        if response.status_code == 200:
            return Availability.TAKEN
        logger.warning(
            "unexpected rdap status", extra={"domain": domain, "status": response.status_code}
        )
        return Availability.UNKNOWN
