"""Document upload / listing / semantic search schemas (§7.7 #6–8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.enums import Classification, DocumentStatus, Language
from app.schemas.common import DanahModel


class DocumentOut(DanahModel):
    id: uuid.UUID
    title: str
    filename: str
    mime_type: str
    language: Language
    classification: Classification
    status: DocumentStatus
    chunk_count: int
    error: str | None = None
    uploaded_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class DocumentUploadResponse(DanahModel):
    id: uuid.UUID
    title: str
    filename: str
    status: DocumentStatus
    message: str = Field(
        default="Document accepted. Indexing runs in the background; poll GET /api/knowledge/documents for status.",
    )


class SearchRequest(DanahModel):
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    k: Annotated[int, Field(ge=1, le=50)] = 8
    language: Language | None = None
    hybrid: bool | None = Field(
        default=None, description="Override HYBRID_SEARCH_ENABLED for this query"
    )


class SearchHit(DanahModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    chunk_index: int
    content: str
    score: float = Field(description="Fused relevance score, 0–1")
    vector_score: float | None = None
    keyword_score: float | None = None
    classification: Classification


class SearchResponse(DanahModel):
    query: str
    hits: list[SearchHit]
    total: int
    hybrid: bool = Field(description="Whether keyword fusion was applied")
