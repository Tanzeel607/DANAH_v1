"""In-app notifications (§7.8). Addressed to a specific user OR to a whole role."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import NotificationKind, Role
from app.models.base import pg_enum


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notifications"

    # Exactly one of user_id / role is set — enforced by the check constraint below.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[Role | None] = mapped_column(pg_enum(Role, "role"), index=True)
    kind: Mapped[NotificationKind] = mapped_column(
        pg_enum(NotificationKind, "notification_kind"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    # {"type": "insight"|"briefing"|"approval"|"source", "id": "<uuid>"}
    subject_ref: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    read_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), index=True)

    __table_args__ = (
        sa.CheckConstraint(
            "(user_id IS NOT NULL AND role IS NULL) OR (user_id IS NULL AND role IS NOT NULL)",
            name="target_user_xor_role",
        ),
    )
