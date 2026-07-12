"""Insight schemas — risk, opportunity, policy (§7.7 #15–16)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import Field, computed_field

from app.enums import AgentName, Classification, InsightKind, Language, PublicationStatus
from app.schemas.common import Citation, DanahModel


class Recommendation(DanahModel):
    action: str
    rationale: str = ""
    owner: str | None = Field(default=None, description="Suggested owning function, if any")
    horizon: str | None = Field(default=None, description="e.g. 'immediate', '30 days', '6 months'")


class InsightOut(DanahModel):
    id: uuid.UUID
    kind: InsightKind
    title: str
    body: str
    severity: int = Field(ge=1, le=5, description="Severity (risk) / impact (opportunity), 1–5")
    likelihood: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    domains: list[str] = []
    recommendations: list[Recommendation] = []
    citations: list[Citation] = []
    language: Language
    classification: Classification
    status: PublicationStatus
    run_id: uuid.UUID | None = None
    created_by_agent: AgentName
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def impact(self) -> int:
        """Mirror of `severity` under the name the UI uses for opportunities."""
        return self.severity


class PolicyDetail(DanahModel):
    """Extra fields the Policy Agent emits (stored in `insights.extra`)."""

    what_changed: str = ""
    jurisdictions: list[str] = []
    compliance_impact: str = ""
    required_response: str = ""
    deadline: date | None = None


class InsightDetail(InsightOut):
    extra: dict[str, Any] = Field(default_factory=dict)
    policy: PolicyDetail | None = Field(
        default=None, description="Populated only when kind == 'policy'"
    )
    approval_id: uuid.UUID | None = None
    approval_status: str | None = None


class InsightFilters(DanahModel):
    kind: InsightKind | None = None
    status: PublicationStatus | None = None
    min_severity: Annotated[int, Field(ge=1, le=5)] | None = None
    domain: str | None = None
    run_id: uuid.UUID | None = None
    q: str | None = None
