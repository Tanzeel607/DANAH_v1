"""Risk / Opportunity / Policy insights produced by the agents.

Publication contract (architecture §4): an agent may only ever create a `draft`, which the
orchestrator immediately moves to `pending_approval`. Only a human decision on the linked
`approvals` row can set `published`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import AgentName, Classification, InsightKind, Language, PublicationStatus
from app.models.base import pg_enum

if TYPE_CHECKING:
    from app.models.pipeline import PipelineRun


class Insight(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "insights"

    kind: Mapped[InsightKind] = mapped_column(
        pg_enum(InsightKind, "insight_kind"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Severity for risks; the same 1–5 scale carries *impact* for opportunities and
    # *compliance impact* for policy insights. One column keeps `GET /insights?severity=`
    # (§7.7 #15) a single indexed filter across all three kinds.
    severity: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, index=True)
    likelihood: Mapped[float | None] = mapped_column(sa.Float)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    domains: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String(100)),
        nullable=False,
        default=list,
        server_default=sa.text("'{}'::varchar[]"),
    )
    recommendations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    # {"chunks": [chunk_id...], "items": [item_id...], "sources": [{n, label, ref}...]}
    citations: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    language: Mapped[Language] = mapped_column(
        pg_enum(Language, "language"), nullable=False, default=Language.EN
    )
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
    created_by_agent: Mapped[AgentName] = mapped_column(
        pg_enum(AgentName, "agent_name"), nullable=False
    )
    # Policy-specific detail (jurisdictions, deadline, required response) lives here rather
    # than in three mostly-null columns.
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )

    run: Mapped[PipelineRun | None] = relationship(back_populates="insights")

    __table_args__ = (
        sa.CheckConstraint("severity >= 1 AND severity <= 5", name="severity_range"),
        sa.CheckConstraint(
            "likelihood IS NULL OR (likelihood >= 0 AND likelihood <= 1)",
            name="likelihood_range",
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )
