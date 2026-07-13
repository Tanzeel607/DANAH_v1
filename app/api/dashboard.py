"""Dashboard API (§7.7 #21). Mounted at /api/dashboard.

One call fills the entire command centre. Every figure in it — including every *count* — is
computed at the caller's clearance: "3 risks you cannot open" is itself a disclosure.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_config, get_current_user, get_db
from app.models import User
from app.schemas.dashboard import DashboardSummary
from app.services.dashboard_service import dashboard_summary

router = APIRouter(tags=["dashboard"])


@router.get(
    "/summary",
    response_model=DashboardSummary,
    summary="Everything the command centre renders, in one call",
    description=(
        "`counts`, `latest_run`, `latest_briefing`, `top_insights`, `source_health`, `cost` "
        "(today / 7-day / threshold) and the `kpi` row — the same KPI figures the Briefing Agent "
        "reads, so the briefing and the dashboard cannot disagree.\n\n"
        "Two users at different clearances legitimately receive different numbers."
    ),
)
async def summary(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_config),
) -> DashboardSummary:
    return await dashboard_summary(db, user=user, settings=settings)
