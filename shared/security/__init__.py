"""JWT access-token validation shared by every resource service.

Tokens are issued by ``auth-service`` (SimpleJWT). Resource services validate
them locally (signature, expiry, issuer, audience, token type) so that a
compromised or misconfigured gateway is never the only line of defence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import jwt

__all__ = ["STAFF_ROLES", "Principal", "TokenError", "decode_access_token"]

STAFF_ROLES = frozenset({"staff", "admin"})


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or not trusted."""


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    role: str = "customer"
    email: str | None = None
    claims: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES


def decode_access_token(
    token: str,
    *,
    secret: str,
    issuer: str,
    audience: str,
    algorithms: tuple[str, ...] = ("HS256",),
    leeway_seconds: int = 10,
) -> Principal:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=list(algorithms),
            issuer=issuer,
            audience=audience,
            leeway=leeway_seconds,
            options={"require": ["exp", "iat", "sub", "token_type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.PyJWTError as exc:
        raise TokenError(f"invalid token: {exc}") from exc

    if claims.get("token_type") != "access":
        raise TokenError("not an access token")

    return Principal(
        user_id=str(claims["sub"]),
        role=str(claims.get("role", "customer")),
        email=claims.get("email"),
        claims=claims,
    )
