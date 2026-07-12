"""Institutional memory schemas (§7.7 #22)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from app.enums import Classification, MemoryKind
from app.schemas.common import DanahModel


class MemoryEntryOut(DanahModel):
    id: uuid.UUID
    kind: MemoryKind
    title: str
    content: str
    tags: list[str] = []
    source_ref: dict[str, Any] = Field(default_factory=dict)
    classification: Classification
    created_by: uuid.UUID | None = None
    created_at: datetime


class MemoryCreate(DanahModel):
    kind: MemoryKind
    title: Annotated[str, Field(min_length=1, max_length=500)]
    content: Annotated[str, Field(min_length=1, max_length=20000)]
    tags: list[str] = []
    source_ref: dict[str, Any] = Field(default_factory=dict)
    classification: Classification = Classification.OFFICIAL


class MemorySearchRequest(DanahModel):
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    k: Annotated[int, Field(ge=1, le=50)] = 8
    kind: MemoryKind | None = None


class MemoryHit(MemoryEntryOut):
    score: float = Field(description="Cosine similarity, 0–1")


class MemorySearchResponse(DanahModel):
    query: str
    hits: list[MemoryHit]
    total: int
