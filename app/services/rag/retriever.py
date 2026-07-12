"""Hybrid retrieval over the document corpus.

Two arms, fused:
  * **Vector** — pgvector cosine similarity against the HNSW index. Finds semantic matches
    ("data residency" ↔ "keep workloads within national borders").
  * **Keyword** — Postgres full-text search. Finds the exact names, codes and numbers that an
    embedding routinely misses ("NY.GDP.MKTP.KD.ZG", "Article 14(b)", "40,000 civil servants").

They are combined with Reciprocal Rank Fusion, which needs no score calibration between two
scales that are not comparable — a cosine similarity and a `ts_rank` cannot be averaged
meaningfully, but their *ranks* can be.

**Classification is a WHERE clause, never a post-filter.** The caller's clearance ceiling is
bound into the SQL, so a chunk above their clearance is never read out of the database, never
enters a prompt, and never sits in process memory (docs/DECISIONS.md #15).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import Float, Select, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.enums import Classification, Language, classification_at_or_below
from app.models import Document, DocumentChunk
from app.services.rag.embeddings import Embedder

log = structlog.get_logger(__name__)

# RRF damping constant. 60 is the value from the original Cormack et al. paper and is the de
# facto default; it stops a single arm's top hit from dominating the fused ranking.
RRF_K = 60


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    chunk_index: int
    content: str
    classification: Classification
    score: float
    vector_score: float | None = None
    keyword_score: float | None = None

    def snippet(self, limit: int = 320) -> str:
        text = " ".join(self.content.split())
        return text if len(text) <= limit else text[:limit].rstrip() + "…"


class Retriever:
    def __init__(self, embedder: Embedder, settings: Settings | None = None) -> None:
        self.embedder = embedder
        self.settings = settings or get_settings()

    async def retrieve(
        self,
        session: AsyncSession,
        query: str,
        *,
        k: int | None = None,
        classification_ceiling: Classification = Classification.INTERNAL,
        language: Language | None = None,
        hybrid: bool | None = None,
        min_score: float | None = None,
        document_ids: list[uuid.UUID] | None = None,
    ) -> list[RetrievedChunk]:
        cfg = self.settings
        k = k or cfg.retrieval_top_k
        use_hybrid = cfg.hybrid_search_enabled if hybrid is None else hybrid
        floor = cfg.retrieval_min_score if min_score is None else min_score

        allowed = classification_at_or_below(classification_ceiling)

        # Over-fetch each arm: fusion re-ranks, so the final top-k should be chosen from a
        # wider candidate pool than k.
        fetch = max(k * 3, 20)

        vector_hits = await self._vector_search(
            session,
            query,
            limit=fetch,
            allowed=allowed,
            language=language,
            document_ids=document_ids,
        )

        keyword_hits: list[RetrievedChunk] = []
        if use_hybrid:
            keyword_hits = await self._keyword_search(
                session,
                query,
                limit=fetch,
                allowed=allowed,
                language=language,
                document_ids=document_ids,
            )

        fused = (
            self._fuse(vector_hits, keyword_hits)
            if keyword_hits
            else [h for h in vector_hits if h.score >= floor]
        )

        results = fused[:k]
        log.info(
            "retrieval",
            query_tokens=len(query.split()),
            vector_hits=len(vector_hits),
            keyword_hits=len(keyword_hits),
            returned=len(results),
            hybrid=use_hybrid,
            ceiling=classification_ceiling.value,
        )
        return results

    # -- arms ----------------------------------------------------------------
    async def _vector_search(
        self,
        session: AsyncSession,
        query: str,
        *,
        limit: int,
        allowed: list[Classification],
        language: Language | None,
        document_ids: list[uuid.UUID] | None,
    ) -> list[RetrievedChunk]:
        embedding = await self.embedder.embed_query(query)

        # pgvector's <=> is cosine DISTANCE in [0, 2]; similarity = 1 - distance.
        distance = DocumentChunk.embedding.cosine_distance(embedding)
        similarity = (1 - distance).label("score")

        stmt: Select[tuple[DocumentChunk, str, float]] = (
            select(DocumentChunk, Document.title, similarity)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.embedding.isnot(None),
                DocumentChunk.classification.in_(allowed),
            )
            .order_by(distance)
            .limit(limit)
        )
        stmt = self._narrow(stmt, language=language, document_ids=document_ids)

        rows = (await session.execute(stmt)).all()
        return [
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                document_title=title,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                classification=chunk.classification,
                score=float(score),
                vector_score=float(score),
            )
            for chunk, title, score in rows
        ]

    async def _keyword_search(
        self,
        session: AsyncSession,
        query: str,
        *,
        limit: int,
        allowed: list[Classification],
        language: Language | None,
        document_ids: list[uuid.UUID] | None,
    ) -> list[RetrievedChunk]:
        # `websearch_to_tsquery` accepts what a human actually types (quoted phrases, OR, -term)
        # and cannot raise a syntax error on hostile input, unlike `to_tsquery`.
        tsquery = func.websearch_to_tsquery("simple", query)
        rank = cast(func.ts_rank(DocumentChunk.content_tsv, tsquery), Float).label("score")

        stmt: Select[tuple[DocumentChunk, str, float]] = (
            select(DocumentChunk, Document.title, rank)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.content_tsv.op("@@")(tsquery),
                DocumentChunk.classification.in_(allowed),
            )
            .order_by(rank.desc())
            .limit(limit)
        )
        stmt = self._narrow(stmt, language=language, document_ids=document_ids)

        rows = (await session.execute(stmt)).all()
        return [
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                document_title=title,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                classification=chunk.classification,
                score=float(score),
                keyword_score=float(score),
            )
            for chunk, title, score in rows
        ]

    @staticmethod
    def _narrow(
        stmt: Select[tuple[DocumentChunk, str, float]],
        *,
        language: Language | None,
        document_ids: list[uuid.UUID] | None,
    ) -> Select[tuple[DocumentChunk, str, float]]:
        if language is not None:
            stmt = stmt.where(DocumentChunk.language == language)
        if document_ids:
            stmt = stmt.where(DocumentChunk.document_id.in_(document_ids))
        return stmt

    # -- fusion --------------------------------------------------------------
    @staticmethod
    def _fuse(
        vector_hits: list[RetrievedChunk],
        keyword_hits: list[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        """Reciprocal Rank Fusion: score = Σ 1/(RRF_K + rank) over the arms that found the chunk.

        The fused score is normalised to (0, 1] so downstream confidence maths stays on one scale.
        A chunk found by BOTH arms outranks one found by either alone — which is exactly the
        behaviour we want, because agreement between a semantic and a lexical match is strong
        evidence of relevance.
        """
        contributions: dict[uuid.UUID, float] = {}
        merged: dict[uuid.UUID, RetrievedChunk] = {}

        for arm in (vector_hits, keyword_hits):
            for rank, hit in enumerate(arm, start=1):
                contributions[hit.chunk_id] = contributions.get(hit.chunk_id, 0.0) + 1.0 / (
                    RRF_K + rank
                )
                existing = merged.get(hit.chunk_id)
                if existing is None:
                    merged[hit.chunk_id] = hit
                else:
                    # Keep whichever per-arm score each arm reported.
                    existing.vector_score = existing.vector_score or hit.vector_score
                    existing.keyword_score = existing.keyword_score or hit.keyword_score

        # Best attainable RRF score = both arms ranking it first.
        best_possible = 2.0 / (RRF_K + 1)
        for chunk_id, raw in contributions.items():
            merged[chunk_id].score = min(1.0, raw / best_possible)

        return sorted(merged.values(), key=lambda h: h.score, reverse=True)


async def search_ingested_items_text(
    session: AsyncSession,
    query: str,
    *,
    limit: int = 10,
    allowed: list[Classification] | None = None,
) -> list[dict[str, str]]:
    """Keyword search over ingested signal items — backs the agents' `search_ingested_items` tool.

    Items are news/indicator records, not corpus documents, so they carry no embedding; FTS over
    title+summary+content is the right (and cheap) tool.
    """
    from app.models import IngestedItem

    tsquery = func.websearch_to_tsquery("simple", query)
    stmt = (
        select(IngestedItem)
        .where(
            or_(
                IngestedItem.content_tsv.op("@@")(tsquery),
                IngestedItem.title.ilike(f"%{query}%"),
            )
        )
        .order_by(func.ts_rank(IngestedItem.content_tsv, tsquery).desc())
        .limit(limit)
    )
    if allowed:
        stmt = stmt.where(IngestedItem.classification.in_(allowed))

    items = (await session.scalars(stmt)).all()
    return [
        {
            "id": str(item.id),
            "title": item.title,
            "summary": (item.summary or item.content or "")[:500],
            "url": item.url or "",
            "published_at": item.published_at.isoformat() if item.published_at else "",
        }
        for item in items
    ]
