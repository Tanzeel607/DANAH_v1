"""Typed agent outputs.

These schemas are the contract between an agent and the rest of the system. The model must
produce exactly this shape or its step fails — there is no "mostly right" path where a
half-formed insight leaks downstream.

Every constraint here is enforced twice: Pydantic rejects an out-of-range value, and the database
has a matching CHECK constraint. That redundancy is deliberate — the model is an untrusted input.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums import ItemCategory, MemoryKind, Urgency


class AgentModel(BaseModel):
    """Base for agent outputs. Unknown fields are dropped rather than rejected.

    `extra="ignore"` is a deliberate difference from the API schemas (which forbid extras): a
    model that helpfully adds a `"notes"` key should not fail its whole step over it, whereas an
    API client sending an unknown field is a bug worth surfacing.
    """

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Signal Agent
# ---------------------------------------------------------------------------
class ItemTriage(AgentModel):
    item_id: str = Field(description="The id from the evidence block, exactly as given.")
    relevance: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description="0 = irrelevant to ministry strategy; 1 = demands immediate attention."
    )
    category: ItemCategory
    urgency: Urgency
    rationale: str = Field(
        max_length=400, description="One line: why this relevance and urgency. No hedging."
    )

    @field_validator("item_id")
    @classmethod
    def _valid_uuid(cls, v: str) -> str:
        # A model that invents an item id would silently attach triage to nothing.
        uuid.UUID(v)
        return v


class SignalOutput(AgentModel):
    triage: list[ItemTriage] = Field(description="One entry per item supplied. Do not skip items.")


# ---------------------------------------------------------------------------
# Risk / Opportunity / Policy Agents
# ---------------------------------------------------------------------------
class RecommendedAction(AgentModel):
    action: str = Field(max_length=300, description="A specific, decidable action.")
    rationale: str = Field(max_length=500, default="")
    owner: str | None = Field(default=None, description="Suggested owning function.")
    horizon: str | None = Field(
        default=None, description="e.g. 'immediate', '30 days', '6 months'."
    )


class InsightCitation(AgentModel):
    """What this claim rests on. An insight with no citations is not publishable."""

    kind: str = Field(description="'item' for an ingested signal, 'chunk' for a corpus passage.")
    id: str = Field(description="The exact id from the evidence block or a tool result.")

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in {"item", "chunk"}:
            raise ValueError("citation kind must be 'item' or 'chunk'")
        return v

    @field_validator("id")
    @classmethod
    def _valid_uuid(cls, v: str) -> str:
        uuid.UUID(v)
        return v


class DraftInsight(AgentModel):
    title: str = Field(max_length=200, description="Specific and falsifiable. Not a topic label.")
    body: str = Field(
        max_length=4000,
        description="The analysis. Every factual claim carries a [n] marker for its source.",
    )
    severity: Annotated[int, Field(ge=1, le=5)] = Field(
        description="1 = negligible, 5 = severe. For an opportunity this is impact."
    )
    likelihood: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None, description="Probability within the stated horizon. Null if not estimable."
    )
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description="Your confidence in this analysis given the evidence. Be honest: thin "
        "evidence means low confidence, however plausible the story."
    )
    domains: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="Affected domains, e.g. 'energy', 'trade', 'fiscal', 'digital'.",
    )
    recommendations: list[RecommendedAction] = Field(
        default_factory=list, max_length=4, description="2-4 actions. Fewer is better than vaguer."
    )
    citations: list[InsightCitation] = Field(
        default_factory=list,
        description="Every id you relied on. An insight with no citations will be rejected.",
    )


class RiskOutput(AgentModel):
    insights: list[DraftInsight] = Field(
        default_factory=list,
        max_length=8,
        description="Risks the evidence genuinely supports. An empty list is a valid, honest "
        "answer when the items show no material risk — do not manufacture one.",
    )


class OpportunityOutput(AgentModel):
    insights: list[DraftInsight] = Field(default_factory=list, max_length=8)


class PolicyChange(DraftInsight):
    """A policy insight is a risk-shaped insight plus the regulatory specifics."""

    what_changed: str = Field(max_length=600, default="")
    jurisdictions: list[str] = Field(default_factory=list, max_length=10)
    compliance_impact: str = Field(max_length=800, default="")
    required_response: str = Field(max_length=800, default="")
    deadline: date | None = Field(
        default=None, description="Compliance deadline, if the source states one. Never guess."
    )


class PolicyOutput(AgentModel):
    insights: list[PolicyChange] = Field(default_factory=list, max_length=6)


# ---------------------------------------------------------------------------
# Briefing Agent
# ---------------------------------------------------------------------------
class BriefingSectionDraft(AgentModel):
    key: str = Field(
        description="One of: exec_summary, top_risks, top_opportunities, policy_watch, decisions"
    )
    heading: str = Field(max_length=120)
    body: str = Field(max_length=3000)


class BriefingOutput(AgentModel):
    title: str = Field(max_length=200)
    sections: list[BriefingSectionDraft] = Field(
        description="Exactly these five, in order: exec_summary, top_risks, top_opportunities, "
        "policy_watch, decisions."
    )
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    citations: list[InsightCitation] = Field(default_factory=list)


class ArabicRendering(AgentModel):
    """The second pass. A faithful rendering, not a summary and not a paraphrase."""

    title_ar: str = Field(max_length=300)
    sections_ar: list[BriefingSectionDraft] = Field(
        description="Same keys, same order as the English. Headings and bodies in Arabic."
    )


# ---------------------------------------------------------------------------
# Memory Agent
# ---------------------------------------------------------------------------
class MemoryDraft(AgentModel):
    kind: MemoryKind
    title: str = Field(max_length=200)
    content: str = Field(max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=8)


class MemoryOutput(AgentModel):
    entries: list[MemoryDraft] = Field(
        default_factory=list,
        max_length=5,
        description="Only what is genuinely durable. An empty list is correct when the run "
        "produced nothing worth remembering — memory that records everything recalls nothing.",
    )
