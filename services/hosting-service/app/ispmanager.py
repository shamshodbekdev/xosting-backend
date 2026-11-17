"""Async client for the ISPmanager 6 API.

ISPmanager exposes every panel action as ``func=<name>`` on ``/ispmgr``.
Credentials are sent in the POST body (never the query string) so they do not
end up in proxy/access logs. Responses use ``out=json``; errors arrive as
HTTP 200 with a ``doc.error`` object, which we translate into exceptions.

Reference: https://docs.ispmanager.com/ispmanager6/api
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ISPManagerError(Exception):
    def __init__(self, message: str, *, code: str | None = None, obj: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.obj = obj

    @property
    def already_exists(self) -> bool:
        return self.code == "exists"


class ISPManagerUnavailable(ISPManagerError):
    """Transport-level failure (timeouts, 5xx); safe to retry."""


class ISPManagerClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 20.0,
        verify_tls: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = f"{username}:{password}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=5.0),
            verify=verify_tls,
            transport=transport,
            headers={"User-Agent": "xosting-hosting-service/1.0"},
        )

    @property
    def panel_url(self) -> str:
        return f"{self._http.base_url}/ispmgr"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, func: str, **params: Any) -> dict[str, Any]:
        data = {"authinfo": self._auth, "out": "json", "func": func, **params}
        try:
            response = await self._http.post("/ispmgr", data=data)
        except httpx.HTTPError as exc:
            raise ISPManagerUnavailable(f"ISPmanager request failed: {exc}") from exc
        if response.status_code >= 500:
            raise ISPManagerUnavailable(f"ISPmanager returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ISPManagerError(f"ISPmanager returned HTTP {response.status_code}")

        try:
            doc = response.json().get("doc", {})
        except ValueError as exc:
            raise ISPManagerError("ISPmanager returned a non-JSON response") from exc

        error = doc.get("error")
        if error:
            message = (error.get("msg") or {}).get("$") or error.get("$type") or "unknown error"
            logger.warning(
                "ispmanager error",
                extra={"func": func, "code": error.get("$type"), "detail": message},
            )
            raise ISPManagerError(message, code=error.get("$type"), obj=error.get("$object"))
        return doc

    # ---- users ---------------------------------------------------------------
    async def create_user(self, username: str, password: str, preset: str) -> None:
        await self.call(
            "user.edit", sok="ok", name=username, passwd=password, confirm=password, preset=preset
        )

    async def set_password(self, username: str, password: str) -> None:
        await self.call("user.edit", sok="ok", elid=username, passwd=password, confirm=password)

    async def suspend_user(self, username: str) -> None:
        await self.call("user.suspend", elid=username)

    async def resume_user(self, username: str) -> None:
        await self.call("user.resume", elid=username)

    async def delete_user(self, username: str) -> None:
        await self.call("user.delete", elid=username)

    async def list_users(self) -> list[dict[str, Any]]:
        doc = await self.call("user")
        return [
            {
                key: (value.get("$") if isinstance(value, dict) else value)
                for key, value in elem.items()
            }
            for elem in doc.get("elem", [])
        ]

    # ---- web domains ---------------------------------------------------------
    async def create_web_domain(self, domain: str, owner: str, email: str) -> None:
        await self.call(
            "webdomain.edit",
            sok="ok",
            name=domain,
            aliases=f"www.{domain}",
            owner=owner,
            email=email,
        )
