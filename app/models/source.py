"""External sources and the items ingested from them."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import Classification, ConnectorKind, ItemStatus, Language, SourceType
from app.models.base import pg_enum

if TYPE_CHECKING:
    pass


class Source(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(sa.String(200), nullable=False, unique=True)
    type: Mapped[SourceType] = mapped_column(pg_enum(SourceType, "source_type"), nullable=False)
    connector: Mapped[ConnectorKind] = mapped_column(
        pg_enum(ConnectorKind, "connector_kind"), nullable=False
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    credibility_score: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.7, server_default="0.7"
    )
    poll_interval_minutes: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=60, server_default="60"
    )
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(sa.String(500))
    # Per-source HMAC secret for webhook ingestion (§7.5). Falls back to
    # WEBHOOK_HMAC_DEFAULT_SECRET when null.
    hmac_secret: Mapped[str | None] = mapped_column(sa.String(255))

    items: Mapped[list[IngestedItem]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        sa.CheckConstraint(
            "credibility_score >= 0 AND credibility_score <= 1",
            name="credibility_score_range",
        ),
        sa.CheckConstraint("poll_interval_minutes > 0", name="poll_interval_positive"),
    )


class IngestedItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ingested_items"

    source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_id: Mapped[str | None] = mapped_column(sa.String(500))
    title: Mapped[str] = mapped_column(sa.String(1000), nullable=False)
    summary: Mapped[str | None] = mapped_column(sa.Text)
    content: Mapped[str | None] = mapped_column(sa.Text)
    url: Mapped[str | None] = mapped_column(sa.String(2000))
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), index=True)
    language: Mapped[Language] = mapped_column(
        pg_enum(Language, "language"), nullable=False, default=Language.EN
    )
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    # Filled by the Signal Agent: {relevance, category, urgency, rationale}
    triage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    dedup_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False, index=True)
    status: Mapped[ItemStatus] = mapped_column(
        pg_enum(ItemStatus, "item_status"),
        nullable=False,
        default=ItemStatus.NEW,
        index=True,
    )
    classification: Mapped[Classification] = mapped_column(
        pg_enum(Classification, "classification"),
        nullable=False,
        default=Classification.PUBLIC,
        index=True,
    )
    content_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        sa.Computed(
            "to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(summary,'') "
            "|| ' ' || coalesce(content,''))",
            persisted=True,
        ),
    )

    source: Mapped[Source] = relationship(back_populates="items")

    @property
    def relevance(self) -> float | None:
        if not self.triage:
            return None
        value = self.triage.get("relevance")
        return float(value) if isinstance(value, int | float) else None

    @property
    def category(self) -> str | None:
        return None if not self.triage else self.triage.get("category")

    @property
    def urgency(self) -> str | None:
        return None if not self.triage else self.triage.get("urgency")
