"""Auth endpoints (§7.7 #1–3): login, refresh, current user.

Mounted under `/api/auth` by `main.py`, so the paths declared here are relative.

`/login` and `/refresh` are public by necessity — they are what mints a credential. Both are
rate-limited by the middleware added in Phase 4; neither leaks whether an account exists.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import client_ip, get_config, get_current_user, get_db
from app.models import User
from app.schemas.auth import LoginRequest, RefreshRequest, TokenPair, UserOut
from app.schemas.common import ErrorResponse
from app.security.rbac import clearance_for
from app.services import auth_service

router = APIRouter(tags=["auth"])
log = structlog.get_logger(__name__)

_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "Credentials were rejected. The body never says which part was wrong.",
    }
}


def _user_out(user: User) -> UserOut:
    """Clearance is derived from the role, never stored — one source of truth (§7.6)."""
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        clearance=clearance_for(user.role),
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


@router.post(
    "/login",
    response_model=TokenPair,
    status_code=status.HTTP_200_OK,
    summary="Exchange email and password for an access/refresh token pair",
    responses=_UNAUTHORIZED,
)
async def login(
    request: Request,
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_config),
) -> TokenPair:
    """Authenticate with email and password.

    The refresh token is returned in plaintext here and nowhere else — the server keeps only its
    digest. Clients must store it as a credential, not as a session hint.
    """
    # `deps.client_ip`, not `request.client.host`: production puts a load balancer in front of
    # the API, so the peer address is the balancer on every single request. The auth trail would
    # record one identical IP for every failed login in the system and see no brute force at all.
    ip = client_ip(request)
    user = await auth_service.authenticate(
        db,
        settings,
        email=body.email,
        password=body.password,
        ip=ip,
    )
    access_token, refresh_token, expires_in = await auth_service.issue_token_pair(
        db, settings, user
    )
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )


@router.post(
    "/refresh",
    response_model=TokenPair,
    status_code=status.HTTP_200_OK,
    summary="Rotate a refresh token for a new token pair",
    responses=_UNAUTHORIZED,
)
async def refresh(
    request: Request,
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_config),
) -> TokenPair:
    """Spend a refresh token and receive a new pair.

    Refresh tokens are single-use. Replaying one that has already been rotated is treated as a
    stolen credential: every refresh token for that user is revoked and this call fails.
    """
    user, access_token, refresh_token, expires_in = await auth_service.rotate_refresh_token(
        db,
        settings,
        refresh_token=body.refresh_token,
    )
    log.info("token_refreshed", user_id=str(user.id), ip=client_ip(request))
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )


@router.get(
    "/me",
    response_model=UserOut,
    status_code=status.HTTP_200_OK,
    summary="The authenticated user, their role and their clearance ceiling",
    responses=_UNAUTHORIZED,
)
async def me(current_user: User = Depends(get_current_user)) -> UserOut:
    """Identity of the bearer of the access token. Any authenticated role may call this."""
    return _user_out(current_user)
