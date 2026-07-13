"""Approvals API (§7.7 #19–20) — the publication gate. Mounted at /api/approvals.

Nothing an agent writes is ever published automatically. Every insight and briefing enters this
queue, and `POST /{id}/decision` is the only route in DANAH that can set `published`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import client_ip, get_db, require_executive
from app.enums import ApprovalStatus
from app.models import User
from app.schemas.approvals import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalOut,
)
from app.services.approval_service import decide, list_pending

router = APIRouter(tags=["approvals"])


@router.get(
    "",
    response_model=list[ApprovalOut],
    summary="The approval queue (executive+)",
    description=(
        "Defaults to `status=pending`. Each row carries its subject's headline "
        "(`subject_title`, `subject_summary`, `subject_confidence`, `subject_severity`) so the "
        "queue renders without a fetch per row."
    ),
)
async def list_approvals(
    approval_status: ApprovalStatus | None = Query(
        default=ApprovalStatus.PENDING,
        alias="status",
        description="The queue is what is pending; pass another status to review past decisions.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_executive),
) -> list[ApprovalOut]:
    return await list_pending(db, status=approval_status, limit=limit, offset=offset)


@router.post(
    "/{approval_id}/decision",
    response_model=ApprovalDecisionResponse,
    summary="Approve, reject, or request changes (executive+)",
    description=(
        "`approved` publishes the subject (a viewer can then see it); `rejected` hides it; "
        "`changes_requested` returns it to draft with the comment attached.\n\n"
        "A decision is final: a second decision on the same approval returns `409` rather than "
        "silently overwriting the first, because the first is now part of the accountability "
        "record. Every decision is written to the hash-chained audit log with the deciding user "
        "and their IP."
    ),
)
async def decide_approval(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_executive),
) -> ApprovalDecisionResponse:
    return await decide(
        db,
        approval_id=approval_id,
        user=user,
        decision=payload.decision,
        comment=payload.comment,
        ip=client_ip(request),
    )
