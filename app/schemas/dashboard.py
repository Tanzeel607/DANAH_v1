"""Dashboard summary (§7.7 #21) — one call powering the UI's command centre."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.schemas.briefings import BriefingOut
from app.schemas.common import DanahModel
from app.schemas.insights import InsightOut
from app.schemas.pipeline import PipelineRunOut


class DashboardCounts(DanahModel):
    items_total: int = 0
    items_new: int = 0
    items_triaged: int = 0
    insights_total: int = 0
    insights_published: int = 0
    risks_open: int = 0
    opportunities_open: int = 0
    policy_open: int = 0
    approvals_pending: int = 0
    documents_indexed: int = 0
    memory_entries: int = 0


class SourceHealth(DanahModel):
    id: uuid.UUID
    name: str
    connector: str
    enabled: bool
    health: str = Field(description="'healthy' | 'stale' | 'failing' | 'disabled' | 'unknown'")
    last_synced_at: datetime | None = None
    last_status: str | None = None
    items_last_24h: int = 0


class CostSummary(DanahModel):
    today_usd: float = 0.0
    last_7d_usd: float = 0.0
    tokens_today: int = 0
    daily_alert_threshold_usd: float = 0.0
    over_threshold: bool = False


class KpiSnapshot(DanahModel):
    """What `get_kpi_snapshot()` hands the Briefing Agent, and what the UI's KPI row renders."""

    generated_at: datetime
    items_last_24h: int = 0
    high_urgency_items: int = 0
    avg_insight_confidence: float | None = None
    top_domains: list[str] = []
    active_sources: int = 0


class DashboardSummary(DanahModel):
    counts: DashboardCounts
    latest_run: PipelineRunOut | None = None
    latest_briefing: BriefingOut | None = None
    top_insights: list[InsightOut] = []
    source_health: list[SourceHealth] = []
    cost: CostSummary
    kpi: KpiSnapshot
    generated_at: datetime
