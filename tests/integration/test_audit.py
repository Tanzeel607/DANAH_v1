"""Phase 4 acceptance criteria — the hash-chained audit log (master prompt §10).

  * GET /api/audit/verify returns valid: true over >=100 entries
  * tampering with a row in the database makes it return the broken index

The tamper test disables the append-only trigger before writing, because the application cannot
alter `audit_log` at all — that is the point of the trigger. Disabling it is precisely the
privileged, database-level attack the hash chain exists to detect: the trigger stops the
application, and the chain catches the DBA.
"""

from __future__ import annotations

import uuid
from itertools import pairwise
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ActorType, Role
from app.models import AuditLog
from app.services.audit_service import GENESIS_HASH, record_audit, verify_chain


async def write_entries(db: AsyncSession, count: int) -> None:
    for i in range(count):
        await record_audit(
            db,
            action=f"test.action.{i % 7}",
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            subject_type="test",
            subject_id=str(uuid.uuid4()),
            detail={"index": i, "note": "synthetic entry"},
        )
    await db.commit()


class TestHashChain:
    async def test_an_empty_chain_is_valid(self, db: AsyncSession) -> None:
        result = await verify_chain(db)

        assert result.valid is True
        assert result.entries_checked == 0

    async def test_the_first_entry_anchors_to_genesis(self, db: AsyncSession) -> None:
        """Without an anchor, an attacker could truncate the log and start a fresh, consistent chain."""
        await write_entries(db, 1)

        first = await db.scalar(select(AuditLog).order_by(AuditLog.id))

        assert first is not None
        assert first.prev_hash == GENESIS_HASH

    async def test_each_entry_links_to_its_predecessor(self, db: AsyncSession) -> None:
        await write_entries(db, 5)

        entries = list((await db.scalars(select(AuditLog).order_by(AuditLog.id))).all())

        for previous, current in pairwise(entries):
            assert current.prev_hash == previous.entry_hash

    async def test_verify_passes_over_more_than_100_entries(self, db: AsyncSession) -> None:
        """§10 Phase 4: `valid: true` over >=100 entries."""
        await write_entries(db, 120)

        result = await verify_chain(db)

        assert result.valid is True
        assert result.entries_checked >= 100
        assert result.broken_at_id is None

    async def test_tampering_with_a_row_is_detected_and_located(self, db: AsyncSession) -> None:
        """§10 Phase 4: altering a row makes verify report the broken index."""
        await write_entries(db, 20)

        entries = list((await db.scalars(select(AuditLog).order_by(AuditLog.id))).all())
        victim = entries[9]

        # The application CANNOT do this — the trigger forbids it. Disabling the trigger simulates
        # an attacker with database-level privileges, which is exactly the threat the chain covers.
        await db.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update"))
        await db.execute(
            update(AuditLog).where(AuditLog.id == victim.id).values(action="tampered.action")
        )
        await db.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update"))
        await db.commit()

        result = await verify_chain(db)

        assert result.valid is False
        assert result.broken_at_id == victim.id
        assert result.broken_at_index == 9
        assert result.reason is not None
        assert "modified" in result.reason.lower() or "entry_hash" in result.reason.lower()

    async def test_deleting_a_row_is_detected(self, db: AsyncSession) -> None:
        await write_entries(db, 10)

        entries = list((await db.scalars(select(AuditLog).order_by(AuditLog.id))).all())
        victim = entries[4]

        await db.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_delete"))
        await db.execute(text(f"DELETE FROM audit_log WHERE id = {victim.id}"))  # noqa: S608
        await db.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_delete"))
        await db.commit()

        result = await verify_chain(db)

        assert result.valid is False
        # The row AFTER the gap is where the chain visibly breaks.
        assert result.broken_at_id == entries[5].id
        assert result.reason is not None
        assert "deleted" in result.reason.lower() or "prev_hash" in result.reason.lower()

    async def test_the_database_itself_refuses_an_update(self, db: AsyncSession) -> None:
        """Defence in depth: the application cannot rewrite history even if it wanted to."""
        import asyncpg
        import pytest

        await write_entries(db, 1)
        entry = await db.scalar(select(AuditLog).order_by(AuditLog.id))
        assert entry is not None

        with pytest.raises((asyncpg.PostgresError, Exception)) as exc_info:
            await db.execute(
                update(AuditLog).where(AuditLog.id == entry.id).values(action="rewritten")
            )
            await db.flush()

        assert "append-only" in str(exc_info.value).lower()
        await db.rollback()


class TestAuditAPI:
    async def test_verify_endpoint_reports_a_valid_chain(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        await write_entries(db, 110)
        headers = await auth_headers(Role.ADMIN)

        resp = await client.get("/api/audit/verify", headers=headers)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["valid"] is True
        assert body["entries_checked"] >= 100
        assert body["broken_at_id"] is None

    async def test_verify_endpoint_reports_the_broken_index(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        await write_entries(db, 30)
        entries = list((await db.scalars(select(AuditLog).order_by(AuditLog.id))).all())
        victim = entries[12]

        await db.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update"))
        await db.execute(
            update(AuditLog).where(AuditLog.id == victim.id).values(detail={"forged": True})
        )
        await db.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update"))
        await db.commit()

        headers = await auth_headers(Role.ADMIN)
        resp = await client.get("/api/audit/verify", headers=headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is False
        assert body["broken_at_id"] == victim.id
        assert body["broken_at_index"] == 12

    async def test_audit_trail_is_admin_only(self, client: AsyncClient, auth_headers: Any) -> None:
        for role in (Role.VIEWER, Role.ANALYST, Role.EXECUTIVE):
            headers = await auth_headers(role)

            listing = await client.get("/api/audit", headers=headers)
            verify = await client.get("/api/audit/verify", headers=headers)

            assert listing.status_code == 403, f"{role.value} must not read the audit trail"
            assert verify.status_code == 403

    async def test_an_approval_decision_is_audited(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        """Master prompt §7.6: every approval decision is audited."""
        from tests.integration.test_approvals import make_insight, submit

        insight = await make_insight(db)
        approval = await submit(db, insight)

        executive = await auth_headers(Role.EXECUTIVE)
        await client.post(
            f"/api/approvals/{approval.id}/decision",
            headers=executive,
            json={"decision": "approved", "comment": "Endorsed."},
        )

        admin = await auth_headers(Role.ADMIN)
        resp = await client.get("/api/audit", headers=admin)

        assert resp.status_code == 200
        actions = [e["action"] for e in resp.json()["items"]]
        assert any("approval" in a for a in actions), (
            "an approval decision must leave an audit entry naming the deciding user"
        )
