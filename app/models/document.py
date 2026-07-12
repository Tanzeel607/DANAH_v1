"""Uploaded documents and their embedded chunks (the RAG corpus)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import Classification, DocumentStatus, Language
from app.models.base import embedding_dim, pg_enum

if TYPE_CHECKING:
    from app.models.user import User


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"

    title: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    filename: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    storage_path: Mapped[str] = mapped_column(sa.String(1000), nullable=False)
    language: Mapped[Language] = mapped_column(
        pg_enum(Language, "language"), nullable=False, default=Language.EN
    )
    classification: Mapped[Classification] = mapped_column(
        pg_enum(Classification, "classification"),
        nullable=False,
        default=Classification.INTERNAL,
        index=True,
    )
    status: Mapped[DocumentStatus] = mapped_column(
        pg_enum(DocumentStatus, "document_status"),
        nullable=False,
        default=DocumentStatus.PENDING,
        index=True,
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    chunk_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(sa.Text)

    uploader: Mapped[User | None] = relationship(back_populates="documents")
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class DocumentChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One retrievable passage. `classification` is denormalised from the parent document
    so the retriever can filter by clearance in a single indexed WHERE clause without a join
    (docs/DECISIONS.md #15)."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_doc_index"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    token_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(embedding_dim()))
    # `metadata` is reserved on the declarative base, so the attribute is `meta`
    # while the column keeps the name the schema specifies.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    classification: Mapped[Classification] = mapped_column(
        pg_enum(Classification, "classification"),
        nullable=False,
        default=Classification.INTERNAL,
        index=True,
    )
    language: Mapped[Language] = mapped_column(
        pg_enum(Language, "language"), nullable=False, default=Language.EN
    )
    # Generated in the migration: to_tsvector('simple', content). Read-only here.
    content_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, sa.Computed("to_tsvector('simple', content)", persisted=True)
    )

    document: Mapped[Document] = relationship(back_populates="chunks")
