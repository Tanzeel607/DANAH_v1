"""Institutional memory API (§7.7 #22). Mounted at /api/memory.

Memory is what stops the ministry re-proposing what it already tried: the agents read it through
the `get_memory` tool, and these endpoints are the human view of the same store, bounded by the
same clearance filter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, require_analyst
from app.enums import MemoryKind
from app.models import MemoryEntry, User
from app.schemas.memory import (
    MemoryEntryOut,
    MemoryHit,
    MemorySearchRequest,
    MemorySearchResponse,
)
from app.security.rbac import user_clearance
from app.services.memory_service import list_memory, search_memory
from app.services.rag.embeddings import Embedder, get_embedder

router = APIRouter(tags=["memory"])


def _entry_out(entry: MemoryEntry) -> MemoryEntryOut:
    return MemoryEntryOut(
        id=entry.id,
        kind=entry.kind,
        title=entry.title,
        content=entry.content,
        tags=list(entry.tags),
        source_ref=entry.source_ref,
        classification=entry.classification,
        created_by=entry.created_by,
        created_at=entry.created_at,
    )


@router.get(
    "",
    response_model=list[MemoryEntryOut],
    summary="Browse institutional memory (analyst+)",
    description="Decisions, lessons and standing context, newest first, within your clearance.",
)
async def list_entries(
    kind: MemoryKind | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_analyst),
) -> list[MemoryEntryOut]:
    entries = await list_memory(
        db,
        clearance=user_clearance(user),
        kind=kind,
        limit=limit,
        offset=offset,
    )
    return [_entry_out(e) for e in entries]


@router.post(
    "/search",
    response_model=MemorySearchResponse,
    summary="Semantic search over institutional memory (analyst+)",
    description=(
        "Vector recall where entries are embedded, keyword overlap where they are not — so memory "
        "recorded in PENDING-CREDENTIALS mode is still findable. `score` is 0–1."
    ),
)
async def search(
    payload: MemorySearchRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_analyst),
    embedder: Embedder = Depends(get_embedder),
) -> MemorySearchResponse:
    hits = await search_memory(
        db,
        query=payload.query,
        k=payload.k,
        clearance=user_clearance(user),
        embedder=embedder,
        kind=payload.kind,
    )

    return MemorySearchResponse(
        query=payload.query,
        hits=[
            MemoryHit(**_entry_out(entry).model_dump(), score=round(score, 4))
            for entry, score in hits
        ],
        total=len(hits),
    )
