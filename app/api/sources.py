"""Sources API (§7.7 #9–11): registry, health, manual sync. Mounted at /api/sources."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    client_ip,
    get_current_user,
    get_db,
    require_admin,
    require_analyst,
)
from app.enums import ActorType, Classification, classification_at_or_below
from app.exceptions import ConflictError, NotFoundError
from app.models import IngestedItem, Source, User
from app.schemas.sources import SourceCreate, SourceOut, SourceUpdate, SyncResponse
from app.security.rbac import user_clearance
from app.services.audit_service import record_audit
from app.services.ingestion.runner import source_health, sync_source_by_id

log = structlog.get_logger(__name__)

router = APIRouter(tags=["sources"])


async def item_counts_by_source(
    db: AsyncSession, *, clearance: Classification
) -> dict[uuid.UUID, int]:
    """Items per source, counted **within the caller's clearance**.

    The count a viewer sees is the count of the items a viewer could actually open — a bare total
    would tell them how much of this source they are not cleared to read.
    """
    rows = await db.execute(
        select(IngestedItem.source_id, func.count(IngestedItem.id))
        .where(IngestedItem.classification.in_(classification_at_or_below(clearance)))
        .group_by(IngestedItem.source_id)
    )
    return {source_id: int(count) for source_id, count in rows.all()}


async def source_out(source: Source, *, item_count: int) -> SourceOut:
    """`health` is computed by the ingestion runner — the sources panel and the command centre
    must never disagree about whether a feed is healthy, so they call the same function."""
    return SourceOut(
        id=source.id,
        name=source.name,
        type=source.type,
        connector=source.connector,
        config=source.config,
        credibility_score=source.credibility_score,
        poll_interval_minutes=source.poll_interval_minutes,
        enabled=source.enabled,
        last_synced_at=source.last_synced_at,
        last_status=source.last_status,
        created_at=source.created_at,
        health=await source_health(source),
        item_count=item_count,
    )


@router.get(
    "",
    response_model=list[SourceOut],
    summary="List every source with its health and item count",
    description=(
        "`health` is one of `healthy` | `stale` | `failing` | `disabled` | `unknown`, precomputed "
        "so the UI performs no arithmetic. A source is `stale` once it has missed three "
        "consecutive polls."
    ),
)
async def list_sources(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SourceOut]:
    sources = (await db.scalars(select(Source).order_by(Source.name))).all()
    counts = await item_counts_by_source(db, clearance=user_clearance(user))
    return [await source_out(s, item_count=counts.get(s.id, 0)) for s in sources]


@router.post(
    "",
    response_model=SourceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a source (admin)",
    description=(
        "`connector` selects the implementation that polls it. A `custom` source has nothing to "
        "poll — it receives pushes on `POST /api/ingest/webhook/{source_id}` instead, which is "
        "how a licensed feed arrives with no new code path."
    ),
)
async def create_source(
    payload: SourceCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> SourceOut:
    existing = await db.scalar(select(Source.id).where(Source.name == payload.name))
    if existing is not None:
        raise ConflictError(
            "A source with that name already exists.", detail={"name": payload.name}
        )

    source = Source(
        id=uuid.uuid4(),
        name=payload.name,
        type=payload.type,
        connector=payload.connector,
        config=payload.config,
        credibility_score=payload.credibility_score,
        poll_interval_minutes=payload.poll_interval_minutes,
        enabled=payload.enabled,
    )
    db.add(source)
    await db.flush()

    await record_audit(
        db,
        action="source.create",
        actor_type=ActorType.USER,
        actor_id=user.id,
        subject_type="source",
        subject_id=source.id,
        ip=client_ip(request),
        detail={"name": source.name, "connector": source.connector.value},
    )
    log.info("source_created", source_id=str(source.id), connector=source.connector.value)

    return await source_out(source, item_count=0)


@router.patch(
    "/{source_id}",
    response_model=SourceOut,
    summary="Update a source (admin)",
    description="Only the fields present in the body are changed.",
)
async def update_source(
    source_id: uuid.UUID,
    payload: SourceUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> SourceOut:
    source = await db.get(Source, source_id)
    if source is None:
        raise NotFoundError("No such source.", detail={"source_id": str(source_id)})

    # `exclude_unset` separates "not supplied" from "supplied as null": a PATCH that omits
    # `enabled` must not disable the source.
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(source, field, value)
    await db.flush()

    await record_audit(
        db,
        action="source.update",
        actor_type=ActorType.USER,
        actor_id=user.id,
        subject_type="source",
        subject_id=source.id,
        ip=client_ip(request),
        # Field names only: a source's config can hold the API key of a licensed feed.
        detail={"fields": sorted(changes)},
    )
    log.info("source_updated", source_id=str(source.id), fields=sorted(changes))

    counts = await item_counts_by_source(db, clearance=user_clearance(user))
    return await source_out(source, item_count=counts.get(source.id, 0))


@router.post(
    "/{source_id}/sync",
    response_model=SyncResponse,
    summary="Sync a source now (analyst+)",
    description=(
        "Runs the connector synchronously and reports what it fetched. Items deduplicate on "
        "`dedup_hash`, so syncing twice never duplicates the corpus.\n\n"
        "A source-side failure (a feed that 404s, an API that rate-limits) returns `200` with "
        '`status: "error"` and the reason — it is the *source* that failed, not this request.'
    ),
)
async def sync_source_now(
    source_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_analyst),
) -> SyncResponse:
    # A disabled source is still syncable by hand: `enabled` governs the scheduler, and an
    # operator testing a source before turning it on is exactly the workflow this route exists for.
    result = await sync_source_by_id(db, source_id)

    await record_audit(
        db,
        action="source.sync",
        actor_type=ActorType.USER,
        actor_id=user.id,
        subject_type="source",
        subject_id=source_id,
        ip=client_ip(request),
        detail={
            "fetched": result.fetched,
            "created": result.created,
            "duplicates": result.duplicates,
            "status": result.status,
        },
    )
    return result
