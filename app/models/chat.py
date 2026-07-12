"""Grounded chat sessions and messages."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import ChatRole, Language
from app.models.base import pg_enum

if TYPE_CHECKING:
    from app.models.user import User


class ChatSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chat_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(sa.String(300), nullable=False, default="New conversation")

    user: Mapped[User] = relationship(back_populates="chat_sessions")
    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChatMessage.created_at",
    )


class ChatMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chat_messages"

    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[ChatRole] = mapped_column(pg_enum(ChatRole, "chat_role"), nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # [{n, document_id, chunk_id, title, snippet, score}, ...]
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    confidence: Mapped[float | None] = mapped_column(sa.Float)
    language: Mapped[Language] = mapped_column(
        pg_enum(Language, "language"), nullable=False, default=Language.EN
    )
    tokens_in: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    session: Mapped[ChatSession] = relationship(back_populates="messages")
