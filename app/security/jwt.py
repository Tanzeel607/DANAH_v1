"""JWT minting and verification.

Access tokens are short-lived (15 min) and stateless. Refresh tokens are long-lived (14 d),
opaque, **rotating**, and stored only as a SHA-256 digest.

Rotation with reuse detection: presenting a refresh token issues a new pair and revokes the
old row. If an already-revoked token is presented again, that is the signature of a stolen
token being replayed — the entire family for that user is revoked (see `auth_service`).

Key rotation readiness: tokens carry a `kid` header. Today only one key exists
(`JWT_SECRET_KEY`); `_key_for` is the single place that must learn about a keyring.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import jwt
from jwt.exceptions import InvalidTokenError

from app.config import Settings
from app.exceptions import AuthError

TokenType = Literal["access", "refresh"]

ISSUER: Final[str] = "danah"
AUDIENCE: Final[str] = "danah-api"
DEFAULT_KID: Final[str] = "primary"


def _key_for(settings: Settings, kid: str = DEFAULT_KID) -> str:
    """Resolve a key id to its signing secret.

    Single-key today. A keyring lands here (and only here) when JWKS/rotation arrives, so
    tokens minted under the old kid keep verifying through the overlap window.
    """
    if kid != DEFAULT_KID:
        raise AuthError("Unknown token key.")
    return settings.jwt_secret_key.get_secret_value()


def create_access_token(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    role: str,
    email: str,
    expires_delta: timedelta | None = None,
) -> tuple[str, int]:
    """Return `(token, expires_in_seconds)`."""
    now = datetime.now(UTC)
    lifetime = expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    expires_at = now + lifetime

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "email": email,
        "type": "access",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(
        payload,
        _key_for(settings),
        algorithm=settings.jwt_algorithm,
        headers={"kid": DEFAULT_KID},
    )
    return token, int(lifetime.total_seconds())


def decode_access_token(settings: Settings, token: str) -> dict[str, Any]:
    """Verify signature, expiry, issuer, audience and token type.

    Every failure surfaces as the same generic `AuthError` — an attacker learns nothing about
    *why* a token was rejected.
    """
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid", DEFAULT_KID)
        payload: dict[str, Any] = jwt.decode(
            token,
            _key_for(settings, kid),
            algorithms=[settings.jwt_algorithm],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "type"]},
        )
    except InvalidTokenError as exc:
        raise AuthError("Invalid or expired token.") from exc

    if payload.get("type") != "access":
        # A refresh token must never be accepted as a bearer credential.
        raise AuthError("Invalid or expired token.")

    return payload


def refresh_token_expiry(settings: Settings) -> datetime:
    return datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
