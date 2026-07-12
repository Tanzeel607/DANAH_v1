"""Background tasks.

Each task owns its own DB session (a worker has no FastAPI request scope) and re-establishes
the request id, so a job's logs correlate with the API call that enqueued it.

Tasks are thin: resolve arguments, call the service that does the real work, record the
outcome. The business logic lives in `app/services/`.

Tasks are registered as their services land:
  Phase 1 — `embed_document`
  Phase 2 — `sync_source`, `sync_all_due_sources`, `run_pipeline`
  Phase 3 — `daily_brief`, `check_daily_cost`
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text

from app.db import get_session_factory
from app.logging import new_request_id, set_request_id

log = structlog.get_logger(__name__)


def bind_request_id(ctx: dict[str, Any]) -> str:
    """Carry the enqueuing request's id into the job, or mint one for cron-originated work."""
    request_id = ctx.get("request_id") or new_request_id()
    set_request_id(str(request_id))
    return str(request_id)


async def worker_ping(ctx: dict[str, Any]) -> dict[str, Any]:
    """End-to-end proof that the queue is being consumed and the worker can reach Postgres.

    Enqueue it and read back the result to distinguish "the worker is down" from "the job is
    stuck" — see `docs/RUNBOOK.md`. This is the only task with no service behind it, by design.
    """
    bind_request_id(ctx)
    factory = get_session_factory()
    async with factory() as session:
        db_ok = bool(await session.scalar(text("SELECT 1")))

    result = {"ok": db_ok, "at": datetime.now(UTC).isoformat(), "job_id": str(ctx.get("job_id"))}
    log.info("worker_ping", **result)
    return result


# --- Phase 1 ---------------------------------------------------------------
async def embed_document(ctx: dict[str, Any], document_id: str) -> dict[str, Any]:
    """Extract → chunk → embed → index one uploaded document.

    `index_document` records its own failure on the row (status `failed`, reason in `error`), so
    this task does not re-raise: an ARQ retry would re-run an extraction that is deterministically
    going to fail again, and the user already has the reason in the API.
    """
    bind_request_id(ctx)
    from app.services.rag.indexer import index_document

    factory = get_session_factory()
    async with factory() as session:
        result = await index_document(session, uuid.UUID(document_id))
        await session.commit()

    log.info(
        "task_embed_document_done",
        document_id=document_id,
        chunks=result.chunk_count,
        status=result.status.value,
    )
    return {
        "document_id": document_id,
        "chunks": result.chunk_count,
        "status": result.status.value,
        "error": result.error,
    }
