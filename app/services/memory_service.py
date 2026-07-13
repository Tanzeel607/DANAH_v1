"""Institutional memory — durable decisions, lessons and standing context.

Memory is what stops the ministry re-proposing something it already tried, rejected, or learned
from. Two properties follow from that purpose and shape every function here:

* **A memory is never lost to a provider outage.** If the embedder is absent
  (PENDING-CREDENTIALS) or fails, the entry is still written, with `embedding = NULL`. An entry
  that exists but is only findable by keyword is strictly better than an entry that was never
  written because Voyage was down for ninety seconds. The row can be back-filled later; the
  lesson cannot be re-learned.

* **Search degrades, it does not die.** With no vectors to search — no embedder, or no embedded
  rows yet — the query falls back to a keyword scan rather than returning nothing. A silent empty
  result would read to the caller as "the ministry has no relevant experience", which is a far
  more damaging answer than a rough one.

Classification is applied in SQL, in the `WHERE` clause, exactly as in the retriever: an entry
above the caller's clearance is never read out of the database (docs/DECISIONS.md #15).
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Classification, MemoryKind, classification_at_or_below
from app.exceptions import LLMGatewayError
from app.models import MemoryEntry
from app.services.rag.embeddings import Embedder

log = structlog.get_logger(__name__)

# Keyword fallback: the longest term list worth ORing into one query. Beyond this the query stops
# discriminating (every entry matches something) and starts costing.
MAX_KEYWORD_TERMS: Final[int] = 8

# Single characters and one-letter tokens match almost every row; they are noise, not a query.
MIN_TERM_LENGTH: Final[int] = 2

# Over-fetch factor for the keyword arm: the SQL orders by recency, the overlap score is computed
# from the rows themselves, so the candidate pool must be wider than k for ranking to mean anything.
KEYWORD_OVERFETCH: Final[int] = 3


async def create_memory(
    session: AsyncSession,
    *,
    kind: MemoryKind,
    title: str,
    content: str,
    tags: list[str],
    source_ref: dict[str, Any],
    classification: Classification,
    embedder: Embedder | None,
    created_by: uuid.UUID | None,
) -> MemoryEntry:
    """Write one memory entry, embedded for recall where an embedder is available.

    Participates in the caller's transaction (the Memory Agent writes several entries per run and
    they must land, or not land, together).
    """
    entry = MemoryEntry(
        id=uuid.uuid4(),
        kind=kind,
        title=title,
        content=content,
        tags=tags,
        source_ref=source_ref,
        classification=classification,
        created_by=created_by,
        embedding=await _embed(embedder, title=title, content=content),
    )
    session.add(entry)
    await session.flush()

    log.info(
        "memory_created",
        memory_id=str(entry.id),
        kind=kind.value,
        classification=classification.value,
        embedded=entry.embedding is not None,
        tag_count=len(tags),
        created_by=str(created_by) if created_by else None,
        # Title and content are never logged: a lesson learned may quote OFFICIAL-SENSITIVE work.
    )
    return entry


async def search_memory(
    session: AsyncSession,
    *,
    query: str,
    k: int,
    clearance: Classification,
    embedder: Embedder | None,
    kind: MemoryKind | None = None,
) -> list[tuple[MemoryEntry, float]]:
    """Recall the k most relevant entries, as `(entry, score)` with score in [0, 1].

    Semantic search when there is anything to search semantically; keyword overlap otherwise.
    Either way the caller's clearance bounds the query in SQL.
    """
    allowed = classification_at_or_below(clearance)

    if embedder is not None and await _has_embedded_rows(session, allowed=allowed, kind=kind):
        try:
            hits = await _vector_search(
                session, query=query, k=k, allowed=allowed, kind=kind, embedder=embedder
            )
        except LLMGatewayError as exc:
            # The provider is down. Answering from keywords is worse than answering from vectors,
            # and better than telling an executive the ministry has no relevant experience.
            log.warning("memory_vector_search_degraded", error_code=exc.code, mode="keyword")
        else:
            log.info("memory_search", mode="vector", hits=len(hits), clearance=clearance.value)
            return hits

    hits = await _keyword_search(session, query=query, k=k, allowed=allowed, kind=kind)
    log.info("memory_search", mode="keyword", hits=len(hits), clearance=clearance.value)
    return hits


async def list_memory(
    session: AsyncSession,
    *,
    clearance: Classification,
    kind: MemoryKind | None = None,
    limit: int,
    offset: int,
) -> list[MemoryEntry]:
    """Newest first — the browse view behind `GET /api/memory`."""
    stmt = (
        select(MemoryEntry)
        .where(MemoryEntry.classification.in_(classification_at_or_below(clearance)))
        .order_by(MemoryEntry.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if kind is not None:
        stmt = stmt.where(MemoryEntry.kind == kind)

    return list((await session.scalars(stmt)).all())


async def count_memory(
    session: AsyncSession,
    *,
    clearance: Classification,
    kind: MemoryKind | None = None,
) -> int:
    """Total matching entries, for the `Page.total` envelope `GET /api/memory` returns."""
    stmt = (
        select(func.count(MemoryEntry.id))
        .select_from(MemoryEntry)
        .where(MemoryEntry.classification.in_(classification_at_or_below(clearance)))
    )
    if kind is not None:
        stmt = stmt.where(MemoryEntry.kind == kind)

    return int(await session.scalar(stmt) or 0)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
async def _embed(embedder: Embedder | None, *, title: str, content: str) -> list[float] | None:
    """The vector for one entry, or None when embedding is unavailable.

    The title carries most of the retrieval signal ("Sanctions exposure was misjudged in 2024"),
    so it is embedded together with the body rather than the body alone.
    """
    if embedder is None:
        return None

    try:
        vectors = await embedder.embed_documents([f"{title}\n{content}"])
    except LLMGatewayError as exc:
        # Deliberate: the row is still written, unsearchable but not lost. See the module docstring.
        log.warning("memory_embedding_failed", error_code=exc.code, embedded=False)
        return None

    return vectors[0] if vectors else None


async def _has_embedded_rows(
    session: AsyncSession,
    *,
    allowed: list[Classification],
    kind: MemoryKind | None,
) -> bool:
    """Is there anything for a vector search to find, within this clearance and kind?

    Asked before embedding the query, so a corpus with no vectors (fresh install, or every entry
    written while the provider was down) costs no embedding call before falling back.
    """
    stmt = (
        select(MemoryEntry.id)
        .where(
            MemoryEntry.embedding.isnot(None),
            MemoryEntry.classification.in_(allowed),
        )
        .limit(1)
    )
    if kind is not None:
        stmt = stmt.where(MemoryEntry.kind == kind)

    return await session.scalar(stmt) is not None


async def _vector_search(
    session: AsyncSession,
    *,
    query: str,
    k: int,
    allowed: list[Classification],
    kind: MemoryKind | None,
    embedder: Embedder,
) -> list[tuple[MemoryEntry, float]]:
    embedding = await embedder.embed_query(query)

    # pgvector's <=> is cosine DISTANCE in [0, 2]; similarity = 1 - distance.
    distance = MemoryEntry.embedding.cosine_distance(embedding)
    similarity = (1 - distance).label("score")

    stmt = (
        select(MemoryEntry, similarity)
        .where(
            MemoryEntry.embedding.isnot(None),
            MemoryEntry.classification.in_(allowed),
        )
        .order_by(distance)
        .limit(k)
    )
    if kind is not None:
        stmt = stmt.where(MemoryEntry.kind == kind)

    rows = (await session.execute(stmt)).all()
    return [(entry, float(score)) for entry, score in rows]


async def _keyword_search(
    session: AsyncSession,
    *,
    query: str,
    k: int,
    allowed: list[Classification],
    kind: MemoryKind | None,
) -> list[tuple[MemoryEntry, float]]:
    """ILIKE fallback, ranked by how many of the query's terms an entry actually contains.

    The score is term overlap, not cosine similarity — an honest number on the same 0–1 scale
    rather than a fabricated one.
    """
    terms = _terms(query)
    if not terms:
        return []

    matches: list[ColumnElement[bool]] = []
    for term in terms:
        pattern = f"%{_escape_like(term)}%"
        matches.append(
            or_(
                MemoryEntry.title.ilike(pattern, escape="\\"),
                MemoryEntry.content.ilike(pattern, escape="\\"),
            )
        )

    stmt = (
        select(MemoryEntry)
        .where(MemoryEntry.classification.in_(allowed), or_(*matches))
        .order_by(MemoryEntry.created_at.desc())
        .limit(k * KEYWORD_OVERFETCH)
    )
    if kind is not None:
        stmt = stmt.where(MemoryEntry.kind == kind)

    candidates = list((await session.scalars(stmt)).all())

    scored: list[tuple[MemoryEntry, float]] = []
    for entry in candidates:
        haystack = f"{entry.title}\n{entry.content}".lower()
        hits = sum(1 for term in terms if term in haystack)
        scored.append((entry, hits / len(terms)))

    # Recency breaks ties: two entries mentioning the same terms, the newer lesson wins.
    scored.sort(key=lambda pair: (pair[1], pair[0].created_at), reverse=True)
    return scored[:k]


def _terms(query: str) -> list[str]:
    """Distinct, lower-cased query terms, in order, capped at MAX_KEYWORD_TERMS."""
    seen: list[str] = []
    for raw in query.lower().split():
        term = raw.strip(".,;:!?\"'()[]{}")
        if len(term) >= MIN_TERM_LENGTH and term not in seen:
            seen.append(term)
        if len(seen) == MAX_KEYWORD_TERMS:
            break
    return seen


def _escape_like(term: str) -> str:
    """Neutralise LIKE wildcards so a query containing `%` matches a literal `%`."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
