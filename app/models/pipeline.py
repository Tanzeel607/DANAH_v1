"""Pipeline runs and per-agent steps (the orchestrator's audit trail of work)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import AgentName, PipelineTrigger, RunStatus, StepStatus
from app.models.base import pg_enum

if TYPE_CHECKING:
    from app.models.briefing import Briefing
    from app.models.insight import Insight


class PipelineRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pipeline_runs"

    trigger: Mapped[PipelineTrigger] = mapped_column(
        pg_enum(PipelineTrigger, "pipeline_trigger"), nullable=False
    )
    status: Mapped[RunStatus] = mapped_column(
        pg_enum(RunStatus, "run_status"), nullable=False, default=RunStatus.RUNNING, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    steps: Mapped[list[PipelineStep]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PipelineStep.created_at",
    )
    insights: Mapped[list[Insight]] = relationship(back_populates="run")
    briefings: Mapped[list[Briefing]] = relationship(back_populates="run")

    @property
    def total_tokens(self) -> int:
        return sum((s.tokens_in or 0) + (s.tokens_out or 0) for s in self.steps)

    @property
    def total_cost_usd(self) -> Decimal:
        total = Decimal("0")
        for step in self.steps:
            total += step.cost_usd or Decimal("0")
        return total


class PipelineStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One agent's execution within a run — the per-step token/cost ledger the UI reads."""

    __tablename__ = "pipeline_steps"

    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent: Mapped[AgentName] = mapped_column(pg_enum(AgentName, "agent_name"), nullable=False)
    status: Mapped[StepStatus] = mapped_column(
        pg_enum(StepStatus, "step_status"), nullable=False, default=StepStatus.RUNNING
    )
    input_ref: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    output_ref: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    tokens_in: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    latency_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(sa.Text)

    run: Mapped[PipelineRun] = relationship(back_populates="steps")
