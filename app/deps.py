"""FastAPI dependency injection.

Phase 0 provides the database session. Auth / RBAC / clearance dependencies are added in
Phase 1 (`app/security/`) and re-exported from here so routers have a single import site.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """Request-scoped session. Commits on success, rolls back on any exception."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_config() -> Settings:
    return get_settings()
