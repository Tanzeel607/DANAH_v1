"""Source and ingested-item schemas (§7.7 #9–12, #24)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from app.enums import (
    Classification,
    ConnectorKind,
    ItemCategory,
    ItemStatus,
    Language,
    SourceType,
    Urgency,
)
from app.schemas.common import DanahModel


class SourceOut(DanahModel):
    id: uuid.UUID
    name: str
    type: SourceType
    connector: ConnectorKind
    config: dict[str, Any]
    credibility_score: float
    poll_interval_minutes: int
    enabled: bool
    last_synced_at: datetime | None = None
    last_status: str | None = None
    created_at: datetime
    # Display-ready health for the UI's source panel.
    health: str = Field(
        default="unknown", description="'healthy' | 'stale' | 'failing' | 'disabled' | 'unknown'"
    )
    item_count: int = 0


class SourceCreate(DanahModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    type: SourceType
    connector: ConnectorKind
    config: dict[str, Any] = Field(default_factory=dict)
    credibility_score: Annotated[float, Field(ge=0.0, le=1.0)] = 0.7
    poll_interval_minutes: Annotated[int, Field(ge=1, le=10080)] = 60
    enabled: bool = True


class SourceUpdate(DanahModel):
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    config: dict[str, Any] | None = None
    credibility_score: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    poll_interval_minutes: Annotated[int, Field(ge=1, le=10080)] | None = None
    enabled: bool | None = None


class SyncResponse(DanahModel):
    source_id: uuid.UUID
    source_name: str
    fetched: int = Field(description="Items returned by the connector")
    created: int = Field(description="New items persisted after deduplication")
    duplicates: int
    status: str
    error: str | None = None
    synced_at: datetime


class TriageOut(DanahModel):
    """Signal Agent output attached to an item."""

    relevance: float = Field(ge=0.0, le=1.0)
    category: ItemCategory
    urgency: Urgency
    rationale: str


class ItemOut(DanahModel):
    id: uuid.UUID
    source_id: uuid.UUID
    source_name: str = ""
    external_id: str | None = None
    title: str
    summary: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    language: Language
    classification: Classification
    status: ItemStatus
    triage: TriageOut | None = None
    # Flattened triage fields — the UI filters and sorts on these directly.
    relevance: float | None = None
    category: ItemCategory | None = None
    urgency: Urgency | None = None
    created_at: datetime


class ItemDetail(ItemOut):
    content: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class WebhookResponse(DanahModel):
    accepted: int
    duplicates: int
    source_id: uuid.UUID
