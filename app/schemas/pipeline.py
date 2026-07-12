"""Pipeline run/step schemas (§7.7 #13–14).

`GET /api/pipeline/runs/{id}` is what the UI polls for live status, so steps carry their own
token/cost/latency figures — that is the Phase-2 acceptance criterion.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.enums import AgentName, PipelineTrigger, RunStatus, StepStatus
from app.schemas.common import DanahModel


class PipelineRunRequest(DanahModel):
    """Optional narrowing of a manual run."""

    max_items: int | None = Field(
        default=None, ge=1, le=1000, description="Defaults to PIPELINE_MAX_ITEMS_PER_RUN"
    )
    agents: list[AgentName] | None = Field(
        default=None,
        description="Restrict the run to these agents. Omit for the full Signal→…→Memory cycle.",
    )


class PipelineRunAccepted(DanahModel):
    run_id: uuid.UUID
    status: RunStatus
    message: str = "Pipeline run enqueued. Poll GET /api/pipeline/runs/{run_id} for live status."


class PipelineStepOut(DanahModel):
    id: uuid.UUID
    agent: AgentName
    status: StepStatus
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    error: str | None = None
    input_ref: dict[str, Any] = Field(default_factory=dict)
    output_ref: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PipelineRunOut(DanahModel):
    id: uuid.UUID
    trigger: PipelineTrigger
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    initiated_by: uuid.UUID | None = None
    # Roll-ups so the UI does not have to sum the steps itself.
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    duration_ms: int | None = None
    step_count: int = 0


class PipelineRunDetail(PipelineRunOut):
    steps: list[PipelineStepOut] = []
    insight_count: int = 0
    briefing_count: int = 0
