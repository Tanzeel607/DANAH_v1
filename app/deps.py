"""FastAPI dependency injection.

Authorisation is enforced *here*, in the API layer, via dependencies — never in the client and
never inside a service (master prompt §3.10). A route's signature therefore states its own
security requirement, and OpenAPI renders it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session_factory
from app.enums import Classification, Role
from app.exceptions import AuthError
from app.models import User
from app.security.jwt import decode_access_token
from app.security.rbac import (
    ADMIN_ONLY,
    ANALYST_AND_ABOVE,
    EXECUTIVE_AND_ABOVE,
    assert_role,
    clearance_for,
)

# auto_error=False so a missing header raises our AuthError (structured JSON) rather than
# Starlette's bare 403 with a different body shape.
_bearer = HTTPBearer(auto_error=False, description="JWT access token from POST /api/auth/login")


async def get_db() -> AsyncIterator[AsyncSession]:
    """Request-scoped session. Commits on success, rolls back on any exception."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_config() -> Settings:
    return get_settings()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_config),
) -> User:
    """Resolve the Bearer token to a live user row.

    The database is consulted on every request rather than trusting the JWT claims alone: a
    deactivated or deleted account must lose access immediately, not when its 15-minute access
    token happens to expire.
    """
    if credentials is None or not credentials.credentials:
        raise AuthError("Authentication required.")

    payload = decode_access_token(settings, credentials.credentials)

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise AuthError("Invalid or expired token.") from exc

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthError("Invalid or expired token.")

    return user


def require_role(*roles: Role) -> Callable[[User], Awaitable[User]]:
    """Dependency factory: allow only these roles.

    Usage: `user: User = Depends(require_role(Role.ADMIN))`
    """

    async def _dependency(user: User = Depends(get_current_user)) -> User:
        assert_role(user, *roles)
        return user

    return _dependency


# Pre-built dependencies for the three tiers the endpoint table uses, so routes read as their
# permission ("analyst+") rather than as a role list.
require_analyst = require_role(*ANALYST_AND_ABOVE)
require_executive = require_role(*EXECUTIVE_AND_ABOVE)
require_admin = require_role(*ADMIN_ONLY)


async def get_clearance(user: User = Depends(get_current_user)) -> Classification:
    """The caller's clearance ceiling — bound into SQL filters by the data layer."""
    return clearance_for(user.role)


def client_ip(request: Request) -> str | None:
    """Best-effort client IP for the audit trail.

    `X-Forwarded-For` is honoured because the production topology puts a load balancer in front
    of the API. It is only trustworthy when that proxy is the one setting it — behind an
    untrusted edge, a caller can forge this header, so it is used for audit context, never for
    an authorisation decision.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
