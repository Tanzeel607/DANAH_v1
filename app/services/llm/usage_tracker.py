"""The cost ledger. Every provider call writes exactly one `api_usage` row (§7.1).

The ledger owns its own session rather than borrowing the caller's. If a request fails and its
transaction rolls back, the tokens were still spent — the bill is real, so the record of it must
survive the rollback. This is the one place where a separate transaction is the correct choice
rather than a smell.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session_factory
from app.enums import UsagePurpose
from app.models import ApiUsage

log = structlog.get_logger(__name__)

_PER_MILLION = Decimal("1000000")
_CENT_PRECISION = Decimal("0.000001")


def compute_cost_usd(
    settings: Settings,
    model: str,
    tokens_in: int,
    tokens_out: int,
) -> Decimal:
    """USD cost of one call, from the configured price table (per 1M tokens).

    An unknown model costs 0 rather than raising: a pricing gap must never fail a user's
    request. It shows up as a zero-cost row in the ledger, which is visibly wrong on the
    dashboard and easy to correct by setting `LLM_PRICE_TABLE`.
    """
    price_in, price_out = settings.price_for(model)
    cost = (Decimal(tokens_in) * price_in + Decimal(tokens_out) * price_out) / _PER_MILLION
    return cost.quantize(_CENT_PRECISION, rounding=ROUND_HALF_UP)


async def record_usage(
    *,
    provider: str,
    model: str,
    purpose: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: Decimal,
    latency_ms: int,
    request_id: str | None = None,
    user_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Append one ledger row. Never raises into the caller — a failed *record* of a successful
    LLM call must not turn that call into a failed request."""
    row = ApiUsage(
        id=uuid.uuid4(),
        provider=provider,
        model=model,
        purpose=UsagePurpose(purpose),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        request_id=request_id,
        user_id=user_id,
    )

    try:
        if session is not None:
            session.add(row)
            await session.flush()
            return

        factory = get_session_factory()
        async with factory() as own_session:
            own_session.add(row)
            await own_session.commit()
    except Exception as exc:
        log.error(
            "usage_record_failed",
            provider=provider,
            model=model,
            purpose=purpose,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            error=str(exc),
        )


async def today_cost_usd(session: AsyncSession, *, when: datetime | None = None) -> Decimal:
    """Total spend since midnight UTC — the figure `DAILY_COST_ALERT_USD` is compared against."""
    now = when or datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total = await session.scalar(
        select(func.coalesce(func.sum(ApiUsage.cost_usd), 0)).where(ApiUsage.ts >= start)
    )
    return Decimal(str(total or 0))


async def cost_since(session: AsyncSession, *, days: int) -> Decimal:
    since = datetime.now(UTC) - timedelta(days=days)
    total = await session.scalar(
        select(func.coalesce(func.sum(ApiUsage.cost_usd), 0)).where(ApiUsage.ts >= since)
    )
    return Decimal(str(total or 0))


async def tokens_today(session: AsyncSession) -> int:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    total = await session.scalar(
        select(
            func.coalesce(func.sum(ApiUsage.tokens_in + ApiUsage.tokens_out), 0),
        ).where(ApiUsage.ts >= start)
    )
    return int(total or 0)


async def run_token_total(session: AsyncSession, request_ids: list[str]) -> int:
    """Tokens attributable to a set of request ids — how the orchestrator enforces
    `PIPELINE_TOKEN_BUDGET` across a whole run."""
    if not request_ids:
        return 0
    total = await session.scalar(
        select(func.coalesce(func.sum(ApiUsage.tokens_in + ApiUsage.tokens_out), 0)).where(
            ApiUsage.request_id.in_(request_ids)
        )
    )
    return int(total or 0)


def settings_for_pricing() -> Settings:
    return get_settings()
