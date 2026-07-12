"""Bilingual executive briefings."""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import Classification, PublicationStatus
from app.models.base import pg_enum

if TYPE_CHECKING:
    from app.models.pipeline import PipelineRun


class Briefing(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """`body_ar` is a faithful Arabic rendering produced by a second LLM pass, never a
    machine-translation afterthought (master prompt §7.3.5, §12)."""

    __tablename__ = "briefings"

    date: Mapped[date] = mapped_column(sa.Date, nullable=False, index=True)
    title: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    body_en: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body_ar: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    # [{key, heading_en, heading_ar, body_en, body_ar, items: [...]}, ...]
    sections: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    citations: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    classification: Mapped[Classification] = mapped_column(
        pg_enum(Classification, "classification"),
        nullable=False,
        default=Classification.OFFICIAL,
        index=True,
    )
    status: Mapped[PublicationStatus] = mapped_column(
        pg_enum(PublicationStatus, "publication_status"),
        nullable=False,
        default=PublicationStatus.DRAFT,
        index=True,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("pipeline_runs.id", ondelete="SET NULL"), index=True
    )

    run: Mapped[PipelineRun | None] = relationship(back_populates="briefings")

    __table_args__ = (
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )
