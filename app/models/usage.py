"""LLM cost ledger. Every provider call writes exactly one row (master prompt §7.1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import UsagePurpose
from app.models.base import pg_enum


class ApiUsage(Base):
    """Append-only ledger; `ts` is its creation time (see the note in models/audit.py)."""

    __tablename__ = "api_usage"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    ts: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True
    )
    provider: Mapped[str] = mapped_column(sa.String(50), nullable=False, index=True)
    model: Mapped[str] = mapped_column(sa.String(100), nullable=False, index=True)
    purpose: Mapped[UsagePurpose] = mapped_column(
        pg_enum(UsagePurpose, "usage_purpose"), nullable=False, index=True
    )
    tokens_in: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    latency_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    request_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
