"""Grounded chat schemas (§7.7 #4–5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.enums import ChatRole, Language
from app.schemas.common import Citation, DanahModel


class ChatRequest(DanahModel):
    session_id: uuid.UUID | None = Field(
        default=None, description="Omit to start a new session; the response returns the new id"
    )
    message: Annotated[str, Field(min_length=1, max_length=8000)]
    language: Language | None = Field(
        default=None, description="Answer language. Defaults to the language of the question."
    )


class ChatResponse(DanahModel):
    session_id: uuid.UUID
    message_id: uuid.UUID
    answer: str
    citations: list[Citation]
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Calibrated blend of retrieval similarity and the model's self-report. "
            "See services/rag/composer.py for the formula."
        ),
    )
    grounded: bool = Field(
        description="False when the corpus did not support an answer and the model abstained"
    )
    language: Language
    latency_ms: int
    tokens_in: int
    tokens_out: int


class ChatMessageOut(DanahModel):
    id: uuid.UUID
    role: ChatRole
    content: str
    citations: list[Citation] = []
    confidence: float | None = None
    created_at: datetime


class ChatSessionOut(DanahModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    message_count: int = 0


class ChatSessionDetail(ChatSessionOut):
    messages: list[ChatMessageOut] = []
