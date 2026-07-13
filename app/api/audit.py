"""Audit API (§7.7 #23): the hash-chained trail, and the endpoint that re-walks it.

Mounted at /api/audit. Admin only — the audit trail names who did what, and reading it is itself
a privilege.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, require_admin
from app.models import AuditLog, User
from app.schemas.audit import AuditEntryOut, AuditVerifyResponse
from app.schemas.common import Page
from app.services.audit_service import verify_chain

router = APIRouter(tags=["audit"])


@router.get(
    "",
    response_model=Page[AuditEntryOut],
    summary="Read the audit trail (admin)",
    description=(
        "Newest first. Filter by `action` (prefix match, e.g. `approval`), `actor_id`, "
        "`subject_type`, `subject_id` and a `date_from`/`date_to` range. Each entry carries its "
        "`prev_hash` and `entry_hash`, so a caller can re-verify the chain independently."
    ),
)
async def list_audit(
    action: str | None = Query(default=None, min_length=1, max_length=100),
    actor_id: uuid.UUID | None = Query(default=None),
    subject_type: str | None = Query(default=None, min_length=1, max_length=50),
    subject_id: str | None = Query(default=None, min_length=1, max_length=100),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> Page[AuditEntryOut]:
    clauses: list[ColumnElement[bool]] = []
    if action:
        # Prefix match, so `?action=approval` returns `approval.decision` and anything else the
        # approval flow may come to write, without the caller having to know the full name.
        clauses.append(AuditLog.action.startswith(action))
    if actor_id is not None:
        clauses.append(AuditLog.actor_id == actor_id)
    if subject_type:
        clauses.append(AuditLog.subject_type == subject_type)
    if subject_id:
        clauses.append(AuditLog.subject_id == subject_id)
    if date_from is not None:
        clauses.append(AuditLog.ts >= date_from)
    if date_to is not None:
        clauses.append(AuditLog.ts <= date_to)

    total = await db.scalar(select(func.count(AuditLog.id)).where(*clauses))
    entries = (
        await db.scalars(
            select(AuditLog)
            .where(*clauses)
            .order_by(AuditLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return Page[AuditEntryOut](
        items=[AuditEntryOut.model_validate(e) for e in entries],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/verify",
    response_model=AuditVerifyResponse,
    summary="Re-walk the hash chain and report the first entry that fails (admin)",
    description=(
        "Recomputes `entry_hash = sha256(prev_hash + canonical_json(row))` for every entry from "
        "the genesis anchor. `valid: false` names **the first entry that fails** — the row that "
        "was altered, or the row after a deletion.\n\n"
        "The database already refuses to help: a trigger rejects UPDATE, DELETE and TRUNCATE on "
        "`audit_log`. Only a superuser disabling that trigger can rewrite history, and this is "
        "the endpoint that detects it."
    ),
)
async def verify(
    max_entries: int | None = Query(
        default=None,
        ge=1,
        description=(
            "Verify only the first N entries from the start of the chain. Omit to walk it whole "
            "— this is not pagination: the chain can only be verified from its anchor forward."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> AuditVerifyResponse:
    result = await verify_chain(db, limit=max_entries)

    return AuditVerifyResponse(
        valid=result.valid,
        entries_checked=result.entries_checked,
        broken_at_id=result.broken_at_id,
        broken_at_index=result.broken_at_index,
        reason=result.reason,
        first_id=result.first_id,
        last_id=result.last_id,
        verified_at=datetime.now(UTC),
    )
