"""Liveness / readiness (§7.7 #25). Public — no auth."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import Settings
from app.deps import get_config, get_db
from app.schemas.common import HealthResponse

router = APIRouter(tags=["ops"])
log = structlog.get_logger(__name__)


async def _check_db(db: AsyncSession) -> str:
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        log.warning("healthcheck_db_failed", error=str(exc))
        return "down"
    return "up"


async def _check_redis(settings: Settings) -> str:
    client: Redis | None = None
    try:
        client = Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        await client.ping()
    except Exception as exc:
        log.warning("healthcheck_redis_failed", error=str(exc))
        return "down"
    else:
        return "up"
    finally:
        if client is not None:
            await client.aclose()


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Liveness + dependency status",
    responses={200: {"description": "Service is running; check fields for dependency health"}},
)
async def healthz(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_config),
) -> HealthResponse:
    """Always 200 while the process is alive; the body reports each dependency.

    `llm_configured=false` means PENDING-CREDENTIALS mode: the service runs and every
    non-LLM route works, but chat/agent routes return 503 rather than fabricate answers.
    """
    return HealthResponse(
        status="ok",
        version=__version__,
        environment=settings.app_env.value,
        database=await _check_db(db),
        redis=await _check_redis(settings),
        llm_configured=settings.has_llm_credentials,
        embeddings_configured=settings.has_embedding_credentials,
        time=datetime.now(UTC),
    )
