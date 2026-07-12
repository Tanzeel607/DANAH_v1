"""Shared response primitives.

Wire-contract rules (master prompt §11): flat, UI-friendly shapes; always an `id`; ISO
timestamps; display-ready fields. The v11 HTML front end consumes these directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class DanahModel(BaseModel):
    """Base for every schema: reads from ORM objects, rejects unknown input fields."""

    model_config = ConfigDict(from_attributes=True, extra="forbid", use_enum_values=False)


class ErrorDetail(DanahModel):
    code: str = Field(description="Stable machine-readable error code")
    message: str
    request_id: str
    fields: list[dict[str, str]] | None = None


class ErrorResponse(DanahModel):
    """The single error envelope every failure returns (master prompt §3.6)."""

    error: ErrorDetail


class Page[T](DanahModel):
    """Uniform pagination envelope."""

    items: list[T]
    total: int = Field(description="Total rows matching the filter, ignoring pagination")
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class PaginationParams(DanahModel):
    limit: Annotated[int, Field(ge=1, le=200)] = 50
    offset: Annotated[int, Field(ge=0)] = 0


class Citation(DanahModel):
    """A numbered source the model was told to cite by number.

    `n` matches the `[n]` marker in the generated text, so the UI can hyperlink inline.
    """

    n: int = Field(description="1-based marker matching [n] in the answer text")
    kind: str = Field(description="'chunk' (knowledge base) or 'item' (ingested signal)")
    id: uuid.UUID
    document_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    title: str
    snippet: str = Field(description="Short quoted extract supporting the claim")
    url: str | None = None
    score: float | None = Field(default=None, description="Retrieval similarity, 0–1")


class OkResponse(DanahModel):
    ok: bool = True
    message: str = ""


class HealthResponse(DanahModel):
    status: str
    version: str
    environment: str
    database: str
    redis: str
    llm_configured: bool
    embeddings_configured: bool
    time: datetime


class UsageSummary(DanahModel):
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


def as_dict(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
