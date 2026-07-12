"""Institutional memory — durable decisions, lessons and context, embedded for recall."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import Classification, MemoryKind
from app.models.base import embedding_dim, pg_enum


class MemoryEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memory_entries"

    kind: Mapped[MemoryKind] = mapped_column(
        pg_enum(MemoryKind, "memory_kind"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String(100)),
        nullable=False,
        default=list,
        server_default=sa.text("'{}'::varchar[]"),
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(embedding_dim()))
    # {"run_id": ..., "insight_ids": [...], "item_ids": [...]}
    source_ref: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    classification: Mapped[Classification] = mapped_column(
        pg_enum(Classification, "classification"),
        nullable=False,
        default=Classification.OFFICIAL,
        index=True,
    )
