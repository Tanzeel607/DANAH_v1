"""Hash-chained, append-only audit log.

`entry_hash = sha256(prev_hash + canonical_json(row))`. UPDATE and DELETE are blocked by a
database trigger installed in migration 0001 — the application *cannot* rewrite history even
if it wanted to, which is the point (architecture §8).

This table deliberately has no `updated_at`: it is append-only, so an update timestamp would
be a column that can never change. `ts` is its creation time (master prompt §6).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import ActorType
from app.models.base import pg_enum


class AuditLog(Base):
    __tablename__ = "audit_log"

    # bigserial: the chain is ordered by this monotonic id, so `verify` can re-walk it.
    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True)
    actor_type: Mapped[ActorType] = mapped_column(
        pg_enum(ActorType, "actor_type"), nullable=False, default=ActorType.SYSTEM
    )
    action: Mapped[str] = mapped_column(sa.String(100), nullable=False, index=True)
    subject_type: Mapped[str | None] = mapped_column(sa.String(50), index=True)
    subject_id: Mapped[str | None] = mapped_column(sa.String(100), index=True)
    ip: Mapped[str | None] = mapped_column(INET)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    prev_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuditLog #{self.id} {self.action} {self.entry_hash[:8]}>"
