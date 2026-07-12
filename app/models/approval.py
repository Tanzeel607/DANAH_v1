"""The human-in-the-loop gate. Nothing agent-authored publishes without a row here."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import AgentName, ApprovalStatus, ApprovalSubject, Role
from app.models.base import pg_enum

if TYPE_CHECKING:
    from app.models.user import User


class Approval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "approvals"

    subject_type: Mapped[ApprovalSubject] = mapped_column(
        pg_enum(ApprovalSubject, "approval_subject"), nullable=False, index=True
    )
    # Polymorphic reference: no FK, because it points at either insights or briefings.
    # Integrity is enforced by the approval service, which resolves the subject before writing.
    subject_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    requested_by_agent: Mapped[AgentName] = mapped_column(
        pg_enum(AgentName, "agent_name"), nullable=False
    )
    assigned_role: Mapped[Role] = mapped_column(
        pg_enum(Role, "role"), nullable=False, default=Role.EXECUTIVE
    )
    status: Mapped[ApprovalStatus] = mapped_column(
        pg_enum(ApprovalStatus, "approval_status"),
        nullable=False,
        default=ApprovalStatus.PENDING,
        index=True,
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(sa.Text)

    decider: Mapped[User | None] = relationship()

    __table_args__ = (
        # One live approval per subject; re-submission after `changes_requested` reuses the row.
        sa.UniqueConstraint("subject_type", "subject_id", name="uq_approvals_subject"),
    )
