"""Document indexing: extract → chunk → embed → store.

Runs as the ARQ task `embed_document`, so an upload returns immediately and a 40-page PDF does
not hold an HTTP connection open. The document's `status` column is the state machine the UI
polls: `pending → processing → indexed | failed`.

Failure is recorded, never swallowed: a document that cannot be extracted ends `failed` with the
reason in `error`, which is what `GET /knowledge/documents` shows. Silently indexing zero chunks
would leave the user asking chat about a document it cannot see.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.enums import DocumentStatus, Language
from app.exceptions import NotFoundError, RetrievalError
from app.models import Document, DocumentChunk
from app.services.rag.chunking import chunk_text, detect_language, extract_text
from app.services.rag.embeddings import Embedder, get_embedder
from app.services.rag.storage import read_document

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class IndexResult:
    document_id: uuid.UUID
    status: DocumentStatus
    chunk_count: int
    error: str | None = None


async def index_document(
    session: AsyncSession,
    document_id: uuid.UUID,
    *,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
) -> IndexResult:
    cfg = settings or get_settings()
    embed = embedder or get_embedder()

    document = await session.get(Document, document_id)
    if document is None:
        raise NotFoundError("Document not found.", detail={"document_id": str(document_id)})

    document.status = DocumentStatus.PROCESSING
    document.error = None
    await session.flush()

    try:
        chunk_count = await _index(session, document, embed, cfg)
    except RetrievalError as exc:
        # Expected, explainable failures (unreadable PDF, unsupported type, provider down).
        document.status = DocumentStatus.FAILED
        document.error = exc.message
        document.chunk_count = 0
        await session.flush()
        log.warning(
            "document_index_failed",
            document_id=str(document_id),
            error_code=exc.code,
            # The document's text is never logged — it may be OFFICIAL-SENSITIVE.
            classification=document.classification.value,
        )
        return IndexResult(document_id, DocumentStatus.FAILED, 0, exc.message)
    except Exception as exc:
        document.status = DocumentStatus.FAILED
        document.error = f"Unexpected error during indexing: {type(exc).__name__}"
        document.chunk_count = 0
        await session.flush()
        log.exception("document_index_crashed", document_id=str(document_id))
        return IndexResult(document_id, DocumentStatus.FAILED, 0, document.error)

    document.status = DocumentStatus.INDEXED
    document.chunk_count = chunk_count
    document.error = None
    await session.flush()

    log.info(
        "document_indexed",
        document_id=str(document_id),
        chunks=chunk_count,
        language=document.language.value,
        classification=document.classification.value,
    )
    return IndexResult(document_id, DocumentStatus.INDEXED, chunk_count)


async def _index(
    session: AsyncSession,
    document: Document,
    embedder: Embedder,
    cfg: Settings,
) -> int:
    raw = await read_document(document.storage_path, cfg)
    text = extract_text(raw, filename=document.filename, mime_type=document.mime_type)

    chunks = chunk_text(
        text,
        chunk_size=cfg.chunk_size_tokens,
        overlap=cfg.chunk_overlap_tokens,
    )
    if not chunks:
        raise RetrievalError(
            "The document contained no extractable text.",
            code="empty_document",
        )

    # Language is detected from the extracted text rather than trusted from the upload form —
    # users mislabel, and the FTS/answer-language hint should reflect what is actually inside.
    document.language = Language(detect_language(text))

    vectors = await embedder.embed_documents([c.content for c in chunks])

    # Re-indexing replaces cleanly: without this, a re-uploaded document would collide with the
    # (document_id, chunk_index) unique constraint or leave orphaned chunks behind.
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))

    session.add_all(
        [
            DocumentChunk(
                id=uuid.uuid4(),
                document_id=document.id,
                chunk_index=chunk.index,
                content=chunk.content,
                token_count=chunk.token_count,
                embedding=vector,
                # Denormalised from the parent so the retriever can filter clearance without a join.
                classification=document.classification,
                language=document.language,
                meta={
                    "document_title": document.title,
                    "filename": document.filename,
                    "chunk_index": chunk.index,
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
    )
    await session.flush()
    return len(chunks)


async def reindex_all(
    session: AsyncSession,
    *,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
) -> list[IndexResult]:
    """Re-embed the whole corpus — needed after changing EMBEDDING_MODEL or EMBEDDING_DIM.

    Vectors from different models are not comparable, so a model change without a reindex
    silently degrades every retrieval. `docs/RUNBOOK.md` points here.
    """
    ids = (await session.scalars(select(Document.id))).all()
    results: list[IndexResult] = []
    for document_id in ids:
        results.append(
            await index_document(session, document_id, embedder=embedder, settings=settings)
        )
    return results


def guess_mime_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".html": "text/html",
        ".htm": "text/html",
    }.get(suffix, "application/octet-stream")
