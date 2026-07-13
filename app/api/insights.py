"""Insights API (§7.7 #15–16): risk, opportunity and policy. Mounted at /api/insights.

Two rules are enforced here and nowhere else:

* **A viewer sees published insights only.** The status filter is *overwritten*, not validated —
  honouring a client-supplied `status=draft` from a viewer would let the caller make the
  authorisation decision.
* **Clearance is a WHERE clause.** Insights above the caller's ceiling are never selected.

`build_citation_lookup` / `citations_for` live here because briefings and the dashboard render
the same numbered citations from the same JSONB shape, and one resolver keeps them identical.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from fastapi import APIRouter, Depends, Query
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.enums import (
    ApprovalSubject,
    Classification,
    InsightKind,
    PublicationStatus,
    Role,
    classification_at_or_below,
)
from app.exceptions import NotFoundError
from app.models import Document, DocumentChunk, IngestedItem, Insight, User
from app.schemas.common import Citation, Page
from app.schemas.insights import InsightDetail, InsightOut, PolicyDetail, Recommendation
from app.security.rbac import user_clearance
from app.services.approval_service import approval_for_subject

router = APIRouter(tags=["insights"])

# Long enough to show the sentence a claim rests on, short enough that a citation list is not a
# second copy of the corpus.
SNIPPET_CHARS: Final[int] = 300

# What staff see when they ask for no particular status. `rejected` is excluded: a rejected
# insight is a decision the ministry has already taken, and re-surfacing it in every list is how
# a rejected finding quietly comes back. It is still retrievable by asking for it by name.
_DEFAULT_STAFF_STATUSES: Final[tuple[PublicationStatus, ...]] = (
    PublicationStatus.DRAFT,
    PublicationStatus.PENDING_APPROVAL,
    PublicationStatus.PUBLISHED,
)

CitationKey = tuple[str, uuid.UUID]


@dataclass(frozen=True, slots=True)
class CitationRef:
    """A pointer an agent wrote: `('item' | 'chunk', id)`, numbered as it appears in the text."""

    n: int
    kind: str
    id: uuid.UUID


def parse_citation_refs(raw: Any) -> list[CitationRef]:
    """Read the `citations` JSONB, whichever of its two shapes it holds.

    The agents emit `[{kind, id}, ...]` (services/agents/schemas.py::InsightCitation); the
    persisted column groups them as `{"items": [...], "chunks": [...]}`. Both are read here so
    the API contract does not depend on which one the writer chose.
    """
    refs: list[CitationRef] = []
    seen: set[CitationKey] = set()

    def _add(kind: str, value: Any) -> None:
        if kind not in ("item", "chunk"):
            return
        try:
            ident = uuid.UUID(str(value))
        except (AttributeError, TypeError, ValueError):
            return
        key: CitationKey = (kind, ident)
        if key in seen:
            return
        seen.add(key)
        refs.append(CitationRef(n=len(refs) + 1, kind=kind, id=ident))

    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                _add(str(entry.get("kind", "")), entry.get("id"))
    elif isinstance(raw, dict):
        for ident in raw.get("items", []) or []:
            _add("item", ident)
        for ident in raw.get("chunks", []) or []:
            _add("chunk", ident)
        for entry in raw.get("sources", []) or []:
            if isinstance(entry, dict):
                _add(str(entry.get("kind", "")), entry.get("id"))

    return refs


def _snippet(text: str | None) -> str:
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= SNIPPET_CHARS:
        return collapsed
    return collapsed[:SNIPPET_CHARS].rstrip() + "…"


async def build_citation_lookup(
    db: AsyncSession,
    raws: Iterable[Any],
    *,
    clearance: Classification,
) -> dict[CitationKey, Citation]:
    """Resolve every reference on a *page* of insights in two queries, not two per row.

    A reference the caller may not read simply does not resolve — the underlying item or chunk is
    excluded by the same clearance filter that governs every other read, so an over-classified
    source cannot be inferred from the citation list either.
    """
    refs = [ref for raw in raws for ref in parse_citation_refs(raw)]
    item_ids = [r.id for r in refs if r.kind == "item"]
    chunk_ids = [r.id for r in refs if r.kind == "chunk"]

    allowed = classification_at_or_below(clearance)
    lookup: dict[CitationKey, Citation] = {}

    if item_ids:
        rows = await db.execute(
            select(
                IngestedItem.id,
                IngestedItem.title,
                IngestedItem.summary,
                IngestedItem.content,
                IngestedItem.url,
                IngestedItem.source_id,
            ).where(
                IngestedItem.id.in_(item_ids),
                IngestedItem.classification.in_(allowed),
            )
        )
        for ident, title, summary, content, url, source_id in rows.all():
            lookup[("item", ident)] = Citation(
                n=0,  # replaced per insight: the same item can be [2] here and [5] there
                kind="item",
                id=ident,
                source_id=source_id,
                title=title,
                snippet=_snippet(summary or content),
                url=url,
            )

    if chunk_ids:
        rows = await db.execute(
            select(
                DocumentChunk.id,
                DocumentChunk.document_id,
                DocumentChunk.content,
                Document.title,
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.id.in_(chunk_ids),
                DocumentChunk.classification.in_(allowed),
            )
        )
        for ident, document_id, content, doc_title in rows.all():
            lookup[("chunk", ident)] = Citation(
                n=0,
                kind="chunk",
                id=ident,
                document_id=document_id,
                title=doc_title,
                snippet=_snippet(content),
            )

    return lookup


def citations_for(raw: Any, lookup: dict[CitationKey, Citation]) -> list[Citation]:
    """Number the resolved citations `[1..n]` in the order the agent referenced them."""
    resolved: list[Citation] = []
    for ref in parse_citation_refs(raw):
        citation = lookup.get((ref.kind, ref.id))
        if citation is None:
            continue
        resolved.append(citation.model_copy(update={"n": len(resolved) + 1}))
    return resolved


def visible_statuses(user: User, requested: PublicationStatus | None) -> list[PublicationStatus]:
    """What this caller is allowed to see — never what they asked to see.

    A viewer is pinned to `published`, and a `status=draft` in their query string is discarded
    rather than honoured: the caller does not get to widen their own visibility.
    """
    if user.role is Role.VIEWER:
        return [PublicationStatus.PUBLISHED]
    if requested is not None:
        return [requested]
    return list(_DEFAULT_STAFF_STATUSES)


def _insight_out(insight: Insight, citations: list[Citation]) -> InsightOut:
    return InsightOut(
        id=insight.id,
        kind=insight.kind,
        title=insight.title,
        body=insight.body,
        severity=insight.severity,
        likelihood=insight.likelihood,
        confidence=insight.confidence,
        domains=list(insight.domains),
        recommendations=[
            Recommendation(
                action=str(r.get("action", "")),
                rationale=str(r.get("rationale", "")),
                owner=r.get("owner"),
                horizon=r.get("horizon"),
            )
            for r in insight.recommendations
            if isinstance(r, dict)
        ],
        citations=citations,
        language=insight.language,
        classification=insight.classification,
        status=insight.status,
        run_id=insight.run_id,
        created_by_agent=insight.created_by_agent,
        created_at=insight.created_at,
        updated_at=insight.updated_at,
    )


async def insights_out(
    db: AsyncSession,
    insights: Sequence[Insight],
    *,
    clearance: Classification,
) -> list[InsightOut]:
    """Render a page of insights with their citations resolved — used by the dashboard too."""
    lookup = await build_citation_lookup(db, (i.citations for i in insights), clearance=clearance)
    return [_insight_out(i, citations_for(i.citations, lookup)) for i in insights]


@router.get(
    "",
    response_model=Page[InsightOut],
    summary="List insights (viewers see published only)",
    description=(
        "Filter by `kind`, `status`, `min_severity`, `domain`, `run_id` and free text `q`. "
        "`severity` is 1–5 and carries *impact* for opportunities; it is mirrored as `impact` so "
        "the UI need not branch on kind.\n\n"
        "A `viewer` always receives published insights, whatever `status` they ask for."
    ),
)
async def list_insights(
    kind: InsightKind | None = Query(default=None),
    insight_status: PublicationStatus | None = Query(default=None, alias="status"),
    min_severity: int | None = Query(default=None, ge=1, le=5),
    domain: str | None = Query(default=None, min_length=1, max_length=100),
    run_id: uuid.UUID | None = Query(default=None),
    q: str | None = Query(default=None, min_length=1, max_length=500),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Page[InsightOut]:
    clearance = user_clearance(user)

    clauses: list[ColumnElement[bool]] = [
        Insight.classification.in_(classification_at_or_below(clearance)),
        Insight.status.in_(visible_statuses(user, insight_status)),
    ]
    if kind is not None:
        clauses.append(Insight.kind == kind)
    if min_severity is not None:
        clauses.append(Insight.severity >= min_severity)
    if domain is not None:
        # `domains @> ARRAY[domain]` — an indexable array containment test, not a scan.
        clauses.append(Insight.domains.contains([domain]))
    if run_id is not None:
        clauses.append(Insight.run_id == run_id)
    if q:
        pattern = f"%{q}%"
        clauses.append(or_(Insight.title.ilike(pattern), Insight.body.ilike(pattern)))

    total = await db.scalar(select(func.count(Insight.id)).where(*clauses))

    insights = (
        await db.scalars(
            select(Insight)
            .where(*clauses)
            .order_by(Insight.created_at.desc(), Insight.severity.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return Page[InsightOut](
        items=await insights_out(db, insights, clearance=clearance),
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{insight_id}",
    response_model=InsightDetail,
    summary="One insight, with citations, recommendations and its approval state",
)
async def get_insight(
    insight_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InsightDetail:
    clearance = user_clearance(user)

    insight = (
        await db.scalars(
            select(Insight).where(
                Insight.id == insight_id,
                Insight.classification.in_(classification_at_or_below(clearance)),
                Insight.status.in_(visible_statuses(user, None)),
            )
        )
    ).one_or_none()

    if insight is None:
        # 404 rather than 403: telling a viewer that a draft exists is itself a disclosure.
        raise NotFoundError("No such insight.", detail={"insight_id": str(insight_id)})

    lookup = await build_citation_lookup(db, [insight.citations], clearance=clearance)
    base = _insight_out(insight, citations_for(insight.citations, lookup))

    approval = await approval_for_subject(
        db, subject_type=ApprovalSubject.INSIGHT, subject_id=insight.id
    )

    return InsightDetail(
        # `impact` is a computed mirror of `severity`, not a stored field — it cannot be passed
        # back into the constructor.
        **base.model_dump(exclude={"impact"}),
        extra=insight.extra,
        policy=_policy_detail(insight),
        approval_id=approval.id if approval else None,
        approval_status=approval.status.value if approval else None,
    )


def _policy_detail(insight: Insight) -> PolicyDetail | None:
    """The Policy Agent's regulatory specifics, stored in `insights.extra`."""
    if insight.kind is not InsightKind.POLICY:
        return None

    extra = insight.extra
    deadline = extra.get("deadline")
    return PolicyDetail(
        what_changed=str(extra.get("what_changed", "")),
        jurisdictions=[str(j) for j in extra.get("jurisdictions", []) or []],
        compliance_impact=str(extra.get("compliance_impact", "")),
        required_response=str(extra.get("required_response", "")),
        deadline=deadline,
    )
