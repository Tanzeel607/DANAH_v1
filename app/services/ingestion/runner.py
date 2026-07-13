"""The sync runner: fetch a source, persist what is new, record what happened.

Three decisions are load-bearing here.

**Dedup is done by the database, not by the application.** The obvious implementation — SELECT the
existing `dedup_hash`es, then INSERT the ones that are missing — has a race: two syncs of the same
source overlapping (a scheduled poll and an operator pressing "Sync now") both see an empty SELECT
and both INSERT, and one of them dies on the unique constraint, losing the *whole batch*. An
`INSERT ... ON CONFLICT (dedup_hash) DO NOTHING` is atomic, so the loser simply inserts nothing and
both syncs report honestly. `RETURNING id` yields only the rows that were actually written, which
is where the created/duplicate counts come from — they are observed, not assumed.

**A failing source never aborts a batch sync.** Sources fail constantly and independently: GDELT
rate-limits, a feed 404s, an indicator code is retired. `sync_source_by_id` therefore *returns* an
error `SyncResponse` rather than raising it, so that syncing twelve sources reports twelve
outcomes. A DB failure, by contrast, is not a source failure — it poisons the caller's transaction
and is left to propagate.

**`last_synced_at` advances on failure too.** It records the last *attempt*, which is what a poll
scheduler needs — otherwise a permanently broken source would be retried on every single tick.
That is also precisely why no connector filters its own results on `since`: the timestamp is not a
watermark of what was successfully ingested.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ItemStatus
from app.exceptions import IngestionError, NotFoundError
from app.metrics import INGESTED_ITEMS
from app.models import IngestedItem, Source
from app.schemas.sources import SyncResponse
from app.services.ingestion.base_connector import BaseConnector, RawItem, build_connector

log = structlog.get_logger(__name__)

# Mirrors the column widths in app/models/source.py. A single over-long title from one feed would
# otherwise abort the INSERT for the entire batch, so values are trimmed rather than trusted.
TITLE_LIMIT: Final = 1_000
URL_LIMIT: Final = 2_000
EXTERNAL_ID_LIMIT: Final = 500
STATUS_LIMIT: Final = 500

# A source is stale once it has missed this many consecutive polls — one late poll is noise, three
# is a pattern worth showing an operator.
STALE_INTERVAL_MULTIPLIER: Final = 3

STATUS_OK: Final = "ok"
STATUS_ERROR: Final = "error"


async def sync_source_by_id(session: AsyncSession, source_id: uuid.UUID) -> SyncResponse:
    """Load a source by id and sync it. The entry point for the scheduler, which only holds ids."""
    source = await session.get(Source, source_id)
    if source is None:
        raise NotFoundError("Source not found.", detail={"source_id": str(source_id)})
    return await sync_source(session, source)


async def sync_source(session: AsyncSession, source: Source) -> SyncResponse:
    """Fetch one source and persist whatever is new. Source failures are returned, never raised.

    Takes the loaded row rather than an id because the caller usually has it already — the sync
    route has to fetch the source anyway to 404 and to reject a disabled one.
    """
    synced_at = datetime.now(UTC)
    items, error = await _fetch(source)

    if error is not None:
        source.last_synced_at = synced_at
        source.last_status = _truncate(f"{STATUS_ERROR}: {error}", STATUS_LIMIT)
        await session.flush()
        log.warning(
            "source_sync_failed",
            source_id=str(source.id),
            connector=source.connector.value,
            # The message describes the *source*, never its content.
            error=error,
        )
        return SyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetched=0,
            created=0,
            duplicates=0,
            status=STATUS_ERROR,
            error=error,
            synced_at=synced_at,
        )

    created, duplicates = await persist_items(session, source, items)
    source.last_synced_at = synced_at
    source.last_status = _truncate(f"{STATUS_OK}: {created} new", STATUS_LIMIT)
    await session.flush()

    log.info(
        "source_synced",
        source_id=str(source.id),
        connector=source.connector.value,
        fetched=len(items),
        created=created,
        duplicates=duplicates,
    )
    return SyncResponse(
        source_id=source.id,
        source_name=source.name,
        fetched=len(items),
        created=created,
        duplicates=duplicates,
        status=STATUS_OK,
        error=None,
        synced_at=synced_at,
    )


async def _fetch(source: Source) -> tuple[list[RawItem], str | None]:
    """Run the connector. Returns (items, error) — exactly one of which is meaningful.

    Nothing in here touches the database, which is what makes the broad `except` safe: an
    unexpected connector bug is contained to its own source instead of poisoning the caller's
    transaction and taking the rest of a batch sync down with it.
    """
    connector: BaseConnector | None = None
    try:
        connector = build_connector(source.connector, source.id, source.config)
        return await connector.fetch(since=source.last_synced_at), None
    except IngestionError as exc:
        return [], exc.message
    except Exception as exc:
        # A malformed payload shape we failed to anticipate. The traceback goes to the log; the
        # source gets a `last_status` an operator can act on, with no internals leaked into it.
        log.exception(
            "connector_crashed",
            source_id=str(source.id),
            connector=source.connector.value,
            error_type=type(exc).__name__,
        )
        return [], f"Unexpected connector failure ({type(exc).__name__})."
    finally:
        if connector is not None:
            await connector.aclose()


async def persist_items(
    session: AsyncSession, source: Source, items: Sequence[RawItem]
) -> tuple[int, int]:
    """Insert items, skipping any whose `dedup_hash` is already present. Returns (created, dupes).

    The single write path for *both* polled and pushed (webhook) items, so a licensed feed
    arriving over a webhook tomorrow deduplicates by exactly the same rule as GDELT does today.
    """
    if not items:
        return 0, 0

    rows: list[dict[str, Any]] = [
        {
            "id": uuid.uuid4(),
            "source_id": source.id,
            "external_id": _truncate(item.external_id, EXTERNAL_ID_LIMIT),
            "title": item.title.strip()[:TITLE_LIMIT],
            "summary": item.summary,
            "content": item.content,
            "url": _truncate(item.url, URL_LIMIT),
            "published_at": item.published_at,
            "language": item.language,
            "classification": item.classification,
            "status": ItemStatus.NEW,
            "raw": item.raw,
            "dedup_hash": item.dedup_hash(source.id),
        }
        for item in items
    ]

    # Race-safe: a concurrent sync that already wrote one of these rows makes this a no-op for
    # that row rather than a unique-violation that would roll back the whole batch. Duplicates
    # *within* this batch collapse the same way.
    statement = (
        pg_insert(IngestedItem)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["dedup_hash"])
        .returning(IngestedItem.id)
    )
    result = await session.execute(statement)
    created = len(result.scalars().all())
    await session.flush()

    INGESTED_ITEMS.labels(connector=source.connector.value).inc(created)
    return created, len(rows) - created


async def due_source_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Enabled sources whose poll interval has elapsed. Never synced counts as due.

    The interval arithmetic is done in SQL against `now()`, so the scheduler compares the source's
    clock (the database's) with itself rather than with a worker's — which may be minutes adrift.
    """
    statement = sa.select(Source.id).where(
        Source.enabled.is_(True),
        sa.or_(
            Source.last_synced_at.is_(None),
            Source.last_synced_at
            # make_interval(years, months, weeks, days, hours, mins, secs) — the poll interval is
            # a per-row column, so the window cannot be a bound parameter.
            < sa.func.now() - sa.func.make_interval(0, 0, 0, 0, 0, Source.poll_interval_minutes),
        ),
    )
    return list((await session.scalars(statement)).all())


async def source_health(source: Source) -> str:
    """Display-ready health for `SourceOut.health` — the source panel's traffic light."""
    if not source.enabled:
        return "disabled"
    if source.last_status and source.last_status.startswith(STATUS_ERROR):
        return "failing"
    if source.last_synced_at is None:
        # Enabled, no failure recorded, but never polled — the scheduler has not reached it yet.
        return "unknown"

    age_seconds = (datetime.now(UTC) - source.last_synced_at).total_seconds()
    stale_after_seconds = source.poll_interval_minutes * STALE_INTERVAL_MULTIPLIER * 60
    if age_seconds > stale_after_seconds:
        return "stale"
    return "healthy"


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    return trimmed[:limit]


__all__ = [
    "due_source_ids",
    "persist_items",
    "source_health",
    "sync_source",
    "sync_source_by_id",
]
