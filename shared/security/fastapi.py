"""FastAPI dependencies built on :func:`decode_access_token`.

Convention: the application stores its settings object on ``app.state.settings``
and that object exposes ``jwt_secret``, ``jwt_issuer`` and ``jwt_audience``.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import Principal, TokenError, decode_access_token

__all__ = ["authenticate_token", "get_current_principal", "require_staff"]

_bearer = HTTPBearer(auto_error=False, description="JWT access token issued by auth-service")


def authenticate_token(token: str, settings: object) -> Principal:
    return decode_access_token(
        token,
        secret=settings.jwt_secret,  # type: ignore[attr-defined]
        issuer=settings.jwt_issuer,  # type: ignore[attr-defined]
        audience=settings.jwt_audience,  # type: ignore[attr-defined]
    )


def get_current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return authenticate_token(credentials.credentials, request.app.state.settings)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_staff(principal: Principal = Depends(get_current_principal)) -> Principal:
    if not principal.is_staff:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Staff only.")
    return principal
