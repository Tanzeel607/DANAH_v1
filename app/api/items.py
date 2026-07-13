"""Ingested items API (§7.7 #12). Mounted at /api/items.

Every read is bounded by the caller's clearance **in SQL** — an item above a viewer's ceiling is
not filtered out of their page, it is never selected (docs/DECISIONS.md #15).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.enums import (
    Classification,
    ItemCategory,
    ItemStatus,
    Urgency,
    classification_at_or_below,
)
from app.exceptions import NotFoundError
from app.models import IngestedItem, Source, User
from app.schemas.common import Page
from app.schemas.sources import ItemDetail, ItemOut, TriageOut
from app.security.rbac import user_clearance

router = APIRouter(tags=["items"])


def _triage_out(triage: dict[str, Any] | None) -> TriageOut | None:
    """Rebuild the Signal Agent's verdict, or nothing.

    A partially-written triage blob yields `None` rather than a half-populated object: the UI
    filters on these fields, and a triage missing its urgency would sort as if it had one.
    """
    if not triage:
        return None
    required = ("relevance", "category", "urgency")
    if any(triage.get(key) is None for key in required):
        return None
    try:
        return TriageOut(
            relevance=float(triage["relevance"]),
            category=ItemCategory(triage["category"]),
            urgency=Urgency(triage["urgency"]),
            rationale=str(triage.get("rationale", "")),
        )
    except (TypeError, ValueError):
        # The column is JSONB written by a model-driven agent; a malformed blob must degrade to
        # "untriaged", never 500 the list endpoint.
        return None


def _item_out(item: IngestedItem, source_name: str) -> ItemOut:
    triage = _triage_out(item.triage)
    return ItemOut(
        id=item.id,
        source_id=item.source_id,
        source_name=source_name,
        external_id=item.external_id,
        title=item.title,
        summary=item.summary,
        url=item.url,
        published_at=item.published_at,
        language=item.language,
        classification=item.classification,
        status=item.status,
        triage=triage,
        relevance=triage.relevance if triage else None,
        category=triage.category if triage else None,
        urgency=triage.urgency if triage else None,
        created_at=item.created_at,
    )


def _filters(
    *,
    clearance: Classification,
    status: ItemStatus | None,
    category: ItemCategory | None,
    urgency: Urgency | None,
    source_id: uuid.UUID | None,
    q: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[ColumnElement[bool]]:
    """The WHERE clause, built once and reused by both the page query and its count."""
    clauses: list[ColumnElement[bool]] = [
        IngestedItem.classification.in_(classification_at_or_below(clearance))
    ]

    if status is not None:
        clauses.append(IngestedItem.status == status)
    if source_id is not None:
        clauses.append(IngestedItem.source_id == source_id)
    if category is not None:
        clauses.append(IngestedItem.triage["category"].astext == category.value)
    if urgency is not None:
        clauses.append(IngestedItem.triage["urgency"].astext == urgency.value)
    if q:
        # The generated `content_tsv` column covers title + summary + content, so this is one
        # index lookup rather than three ILIKEs over the corpus.
        clauses.append(IngestedItem.content_tsv.op("@@")(func.websearch_to_tsquery("simple", q)))

    # Items from feeds that publish no date still have to fall inside a date filter, so the
    # range is applied to the date the UI actually displays.
    dated = func.coalesce(IngestedItem.published_at, IngestedItem.created_at)
    if date_from is not None:
        clauses.append(dated >= date_from)
    if date_to is not None:
        clauses.append(dated <= date_to)

    return clauses


@router.get(
    "",
    response_model=Page[ItemOut],
    summary="List ingested items with their Signal-Agent triage",
    description=(
        "Filter by `status`, `category`, `urgency`, `source_id`, free text `q` (full-text over "
        "title, summary and body) and a `date_from`/`date_to` range. Triage fields are flattened "
        "onto the item so the UI can sort on them without unpacking."
    ),
)
async def list_items(
    status: ItemStatus | None = Query(default=None),
    category: ItemCategory | None = Query(default=None),
    urgency: Urgency | None = Query(default=None),
    source_id: uuid.UUID | None = Query(default=None),
    q: str | None = Query(default=None, min_length=1, max_length=500),
    date_from: datetime | None = Query(default=None, description="Inclusive lower bound"),
    date_to: datetime | None = Query(default=None, description="Inclusive upper bound"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Page[ItemOut]:
    clauses = _filters(
        clearance=user_clearance(user),
        status=status,
        category=category,
        urgency=urgency,
        source_id=source_id,
        q=q,
        date_from=date_from,
        date_to=date_to,
    )

    total = await db.scalar(select(func.count(IngestedItem.id)).where(*clauses))

    stmt: Select[tuple[IngestedItem, str]] = (
        select(IngestedItem, Source.name)
        .join(Source, Source.id == IngestedItem.source_id)
        .where(*clauses)
        .order_by(
            IngestedItem.published_at.desc().nullslast(),
            IngestedItem.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).all()

    return Page[ItemOut](
        items=[_item_out(item, source_name) for item, source_name in rows],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{item_id}",
    response_model=ItemDetail,
    summary="One item, including its raw source payload",
    description=(
        "An item above your clearance returns 404, not 403: confirming that it exists would "
        "already leak what the classification is protecting."
    ),
)
async def get_item(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ItemDetail:
    row = (
        await db.execute(
            select(IngestedItem, Source.name)
            .join(Source, Source.id == IngestedItem.source_id)
            .where(
                IngestedItem.id == item_id,
                # Clearance is part of the lookup, not a check applied to the result.
                IngestedItem.classification.in_(classification_at_or_below(user_clearance(user))),
            )
        )
    ).one_or_none()

    if row is None:
        raise NotFoundError("No such item.", detail={"item_id": str(item_id)})

    item, source_name = row
    base = _item_out(item, source_name)
    return ItemDetail(**base.model_dump(), content=item.content, raw=item.raw)
