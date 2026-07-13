"""Briefings API (§7.7 #17–18): bilingual executive briefings. Mounted at /api/briefings.

`body_ar` is a product requirement, not a nicety (master prompt §12): the detail endpoint returns
English **and** Arabic, always. A briefing whose Arabic pass failed does not become an
English-only briefing — the Briefing Agent fails the step instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import structlog
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.insights import build_citation_lookup, citations_for, visible_statuses
from app.config import Settings
from app.deps import client_ip, get_config, get_current_user, get_db, require_executive
from app.enums import ActorType, ApprovalSubject, Classification, classification_at_or_below
from app.exceptions import NotFoundError
from app.models import Briefing, User
from app.schemas.briefings import (
    BriefingDetail,
    BriefingGenerateRequest,
    BriefingOut,
    BriefingSection,
)
from app.security.rbac import user_clearance
from app.services.approval_service import approval_for_subject
from app.services.audit_service import record_audit
from app.services.llm.gateway import LLMGateway, get_gateway
from app.services.rag.embeddings import Embedder, get_embedder

log = structlog.get_logger(__name__)

router = APIRouter(tags=["briefings"])


def _visibility(user: User) -> list[ColumnElement[bool]]:
    """Clearance and publication state, both as WHERE clauses. A viewer sees published only."""
    return [
        Briefing.classification.in_(classification_at_or_below(user_clearance(user))),
        Briefing.status.in_(visible_statuses(user, None)),
    ]


def _briefing_out(briefing: Briefing) -> BriefingOut:
    return BriefingOut(
        id=briefing.id,
        date=briefing.date,
        title=briefing.title,
        confidence=briefing.confidence,
        classification=briefing.classification,
        status=briefing.status,
        run_id=briefing.run_id,
        created_at=briefing.created_at,
    )


def _sections(raw: Sequence[object]) -> list[BriefingSection]:
    """Read the `sections` JSONB tolerantly.

    The stored blob may carry extra keys the agent added (`items`, for instance) and the wire
    schema forbids extras, so the five contract fields are picked out rather than the object being
    validated wholesale. A section missing its Arabic heading is still rendered — dropping it
    would silently shorten the briefing an executive is reading.

    `Sequence[object]` rather than `list[dict]`: this is JSONB, and the type annotation on the
    column is a promise about the writer, not a guarantee about the row.
    """
    sections: list[BriefingSection] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sections.append(
            BriefingSection(
                key=str(entry.get("key", "")),
                heading_en=str(entry.get("heading_en", "")),
                heading_ar=str(entry.get("heading_ar", "")),
                body_en=str(entry.get("body_en", "")),
                body_ar=str(entry.get("body_ar", "")),
            )
        )
    return sections


async def briefing_detail(
    db: AsyncSession, briefing: Briefing, *, clearance: Classification
) -> BriefingDetail:
    lookup = await build_citation_lookup(db, [briefing.citations], clearance=clearance)
    approval = await approval_for_subject(
        db, subject_type=ApprovalSubject.BRIEFING, subject_id=briefing.id
    )

    return BriefingDetail(
        **_briefing_out(briefing).model_dump(),
        body_en=briefing.body_en,
        body_ar=briefing.body_ar,
        sections=_sections(briefing.sections),
        citations=citations_for(briefing.citations, lookup),
        approval_id=approval.id if approval else None,
        approval_status=approval.status.value if approval else None,
    )


@router.get(
    "",
    response_model=list[BriefingOut],
    summary="List briefings, newest first (viewers see published only)",
)
async def list_briefings(
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[BriefingOut]:
    briefings = (
        await db.scalars(
            select(Briefing)
            .where(*_visibility(user))
            .order_by(Briefing.date.desc(), Briefing.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [_briefing_out(b) for b in briefings]


@router.get(
    "/{briefing_id}",
    response_model=BriefingDetail,
    summary="One briefing, in English and Arabic",
    description=(
        "Returns `body_en` **and** `body_ar`, plus the five sections and the resolved citations. "
        "A briefing above your clearance returns 404: confirming it exists would already leak."
    ),
)
async def get_briefing(
    briefing_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BriefingDetail:
    briefing = (
        await db.scalars(select(Briefing).where(Briefing.id == briefing_id, *_visibility(user)))
    ).one_or_none()

    if briefing is None:
        raise NotFoundError("No such briefing.", detail={"briefing_id": str(briefing_id)})

    return await briefing_detail(db, briefing, clearance=user_clearance(user))


@router.post(
    "/generate",
    response_model=BriefingDetail,
    summary="Generate a briefing on demand (executive+)",
    description=(
        "Runs the Briefing Agent **alone**, against the insights that already exist — it does not "
        "ingest, triage or re-analyse. Returns the briefing synchronously.\n\n"
        "The result is a `draft` in the approval queue, not a published briefing: an on-demand "
        "briefing is subject to exactly the same human gate as a scheduled one. Pass `force` to "
        "regenerate when one already exists for that date."
    ),
)
async def generate_briefing(
    payload: BriefingGenerateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_executive),
    gateway: LLMGateway = Depends(get_gateway),
    embedder: Embedder = Depends(get_embedder),
    settings: Settings = Depends(get_config),
) -> BriefingDetail:
    # Imported inside the handler: the orchestrator pulls in all six agents and the provider SDKs
    # behind them, and only this route and POST /api/pipeline/run need that graph.
    from app.services.orchestrator import generate_briefing_only

    briefing = await generate_briefing_only(
        db,
        user=user,
        gateway=gateway,
        embedder=embedder,
        for_date=payload.date,
        force=payload.force,
        settings=settings,
    )

    await record_audit(
        db,
        action="briefing.generate",
        actor_type=ActorType.USER,
        actor_id=user.id,
        subject_type="briefing",
        subject_id=briefing.id,
        ip=client_ip(request),
        detail={"date": briefing.date.isoformat(), "force": payload.force},
    )
    log.info(
        "briefing_generated",
        briefing_id=str(briefing.id),
        run_id=str(briefing.run_id) if briefing.run_id else None,
        requested_by=str(user.id),
        # The briefing text itself is never logged.
    )

    return await briefing_detail(db, briefing, clearance=user_clearance(user))
