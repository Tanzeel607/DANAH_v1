"""Admin API: user administration. Mounted at /api/admin.

Admin only. Every write here lands in the hash-chained audit log — creating an account, changing
a role and revoking access are exactly the actions an investigation asks about afterwards.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import client_ip, get_db, require_admin
from app.enums import ActorType, Role
from app.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.models import User
from app.schemas.auth import UserCreate, UserOut, UserUpdate
from app.security.passwords import hash_password
from app.security.rbac import clearance_for
from app.services.audit_service import record_audit
from app.services.auth_service import revoke_all_for_user

log = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])


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


@router.get(
    "/users",
    response_model=list[UserOut],
    summary="List users (admin)",
)
async def list_users(
    role: Role | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[UserOut]:
    stmt = select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
    if role is not None:
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))

    users = (await db.scalars(stmt)).all()
    return [_user_out(u) for u in users]


@router.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user (admin)",
    description=(
        "The password is hashed with argon2id and never stored, logged or returned. The new "
        "account's clearance follows from its role and cannot be set independently."
    ),
)
async def create_user(
    payload: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserOut:
    email = payload.email.strip().lower()

    existing = await db.scalar(select(User.id).where(func.lower(User.email) == email))
    if existing is not None:
        raise ConflictError("An account with that email already exists.")

    # argon2id is deliberately expensive (tens of milliseconds of CPU). On the event loop that
    # cost is paid by every other request in flight, so it runs in a worker thread.
    password_hash = await asyncio.to_thread(hash_password, payload.password)

    user = User(
        id=uuid.uuid4(),
        email=email,
        full_name=payload.full_name,
        password_hash=password_hash,
        role=payload.role,
        is_active=True,
    )
    db.add(user)
    await db.flush()

    await record_audit(
        db,
        action="user.create",
        actor_type=ActorType.USER,
        actor_id=admin.id,
        subject_type="user",
        subject_id=user.id,
        ip=client_ip(request),
        # The email identifies the account and belongs in the trail; the password never appears
        # anywhere, in any form.
        detail={"email": user.email, "role": user.role.value},
    )
    log.info("user_created", user_id=str(user.id), role=user.role.value, by=str(admin.id))

    return _user_out(user)


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    summary="Update a user (admin)",
    description=(
        "Changing the password or deactivating an account revokes every refresh token that "
        "account holds — otherwise a 14-day refresh token would outlive the revocation and the "
        "account would keep minting access tokens for a fortnight."
    ),
)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserOut:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError("No such user.", detail={"user_id": str(user_id)})

    changes = payload.model_dump(exclude_unset=True)

    # An admin demoting or disabling *themselves* can lock the last administrator out of the
    # system, and no route exists to undo it from inside. Two admins are required for that.
    if user.id == admin.id and (
        ("role" in changes and changes["role"] != admin.role)
        or ("is_active" in changes and changes["is_active"] is False)
    ):
        raise PermissionDeniedError(
            "You cannot change your own role or deactivate your own account. "
            "Ask another administrator.",
            detail={"user_id": str(user_id)},
        )

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.password is not None:
        user.password_hash = await asyncio.to_thread(hash_password, payload.password)

    # A revoked credential must die now, not when it happens to expire.
    revoked = 0
    if payload.password is not None or payload.is_active is False:
        revoked = await revoke_all_for_user(db, user_id=user.id)

    await db.flush()

    await record_audit(
        db,
        action="user.update",
        actor_type=ActorType.USER,
        actor_id=admin.id,
        subject_type="user",
        subject_id=user.id,
        ip=client_ip(request),
        detail={
            # Field *names* only for the password: that it changed is auditable, its value is not.
            "fields": sorted(changes),
            "role": user.role.value,
            "is_active": user.is_active,
            "refresh_tokens_revoked": revoked,
        },
    )
    log.info(
        "user_updated",
        user_id=str(user.id),
        fields=sorted(changes),
        refresh_tokens_revoked=revoked,
        by=str(admin.id),
    )

    return _user_out(user)
