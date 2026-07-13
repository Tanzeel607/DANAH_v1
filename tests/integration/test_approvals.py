"""Phase 3 acceptance criteria (master prompt §10).

  * everything an agent produces lands in the approvals queue as pending
  * approving publishes it (a viewer can then see it)
  * rejecting hides it
  * memory entries are created and retrievable; notification rows are created

The publication gate is the system's central accountability claim, so these tests attack it from
both sides: nothing publishes without a decision, and a decision cannot be made twice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    AgentName,
    ApprovalStatus,
    ApprovalSubject,
    Classification,
    InsightKind,
    Language,
    MemoryKind,
    NotificationKind,
    PublicationStatus,
    Role,
)
from app.models import Approval, Briefing, Insight, MemoryEntry, Notification


async def make_insight(
    db: AsyncSession,
    *,
    status: PublicationStatus = PublicationStatus.DRAFT,
    kind: InsightKind = InsightKind.RISK,
    classification: Classification = Classification.OFFICIAL,
    title: str = "Concentration risk in semiconductor supply",
) -> Insight:
    insight = Insight(
        id=uuid.uuid4(),
        kind=kind,
        title=title,
        body="78 percent of advanced logic capacity sits in one jurisdiction [1].",
        severity=4,
        likelihood=0.6,
        confidence=0.75,
        domains=["trade"],
        recommendations=[{"action": "Qualify a second source", "rationale": "Reduces exposure"}],
        citations={"items": [str(uuid.uuid4())], "chunks": []},
        language=Language.EN,
        classification=classification,
        status=status,
        created_by_agent=AgentName.RISK,
    )
    db.add(insight)
    await db.flush()
    return insight


async def submit(db: AsyncSession, insight: Insight) -> Approval:
    from app.services.approval_service import submit_for_approval

    approval = await submit_for_approval(
        db,
        subject_type=ApprovalSubject.INSIGHT,
        subject_id=insight.id,
        requested_by_agent=AgentName.RISK,
    )
    await db.commit()
    return approval


class TestApprovalGate:
    async def test_submission_marks_the_subject_pending_and_queues_it(
        self, db: AsyncSession
    ) -> None:
        insight = await make_insight(db)

        approval = await submit(db, insight)

        await db.refresh(insight)
        assert insight.status is PublicationStatus.PENDING_APPROVAL
        assert approval.status is ApprovalStatus.PENDING
        assert approval.subject_id == insight.id

    async def test_approving_publishes_the_subject(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        insight = await make_insight(db)
        approval = await submit(db, insight)
        headers = await auth_headers(Role.EXECUTIVE)

        resp = await client.post(
            f"/api/approvals/{approval.id}/decision",
            headers=headers,
            json={"decision": "approved", "comment": "Endorsed."},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["subject_status"] == PublicationStatus.PUBLISHED.value

        await db.refresh(insight)
        assert insight.status is PublicationStatus.PUBLISHED

    async def test_rejecting_hides_the_subject(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        insight = await make_insight(db)
        approval = await submit(db, insight)
        headers = await auth_headers(Role.EXECUTIVE)

        resp = await client.post(
            f"/api/approvals/{approval.id}/decision",
            headers=headers,
            json={"decision": "rejected", "comment": "Evidence too thin."},
        )

        assert resp.status_code == 200
        await db.refresh(insight)
        assert insight.status is PublicationStatus.REJECTED

    async def test_changes_requested_keeps_it_out_of_sight(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        insight = await make_insight(db)
        approval = await submit(db, insight)
        headers = await auth_headers(Role.EXECUTIVE)

        resp = await client.post(
            f"/api/approvals/{approval.id}/decision",
            headers=headers,
            json={"decision": "changes_requested", "comment": "Add the fiscal impact."},
        )

        assert resp.status_code == 200
        await db.refresh(insight)
        assert insight.status is not PublicationStatus.PUBLISHED

    async def test_a_decision_cannot_be_made_twice(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        """A second decision silently overwriting the first would destroy the accountability trail."""
        insight = await make_insight(db)
        approval = await submit(db, insight)
        headers = await auth_headers(Role.EXECUTIVE)

        first = await client.post(
            f"/api/approvals/{approval.id}/decision",
            headers=headers,
            json={"decision": "approved", "comment": "Endorsed."},
        )
        assert first.status_code == 200

        second = await client.post(
            f"/api/approvals/{approval.id}/decision",
            headers=headers,
            json={"decision": "rejected", "comment": "Changed my mind."},
        )

        assert second.status_code == 409
        await db.refresh(insight)
        assert insight.status is PublicationStatus.PUBLISHED

    async def test_analyst_cannot_approve(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        insight = await make_insight(db)
        approval = await submit(db, insight)
        headers = await auth_headers(Role.ANALYST)

        resp = await client.post(
            f"/api/approvals/{approval.id}/decision",
            headers=headers,
            json={"decision": "approved", "comment": ""},
        )

        assert resp.status_code == 403

    async def test_the_decision_is_recorded_against_the_deciding_user(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any, user_factory: Any
    ) -> None:
        insight = await make_insight(db)
        approval = await submit(db, insight)
        headers = await auth_headers(Role.EXECUTIVE)

        await client.post(
            f"/api/approvals/{approval.id}/decision",
            headers=headers,
            json={"decision": "approved", "comment": "Endorsed by the Under-Secretary."},
        )

        await db.refresh(approval)
        assert approval.decided_by is not None
        assert approval.decided_at is not None
        assert approval.comment == "Endorsed by the Under-Secretary."


class TestViewerSeesPublishedOnly:
    async def test_viewer_cannot_see_a_pending_insight(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        insight = await make_insight(db, classification=Classification.INTERNAL)
        await submit(db, insight)
        headers = await auth_headers(Role.VIEWER)

        resp = await client.get("/api/insights", headers=headers)

        assert resp.status_code == 200
        titles = [i["title"] for i in resp.json()["items"]]
        assert insight.title not in titles

    async def test_viewer_sees_it_once_published(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        insight = await make_insight(db, classification=Classification.INTERNAL)
        approval = await submit(db, insight)

        executive = await auth_headers(Role.EXECUTIVE)
        await client.post(
            f"/api/approvals/{approval.id}/decision",
            headers=executive,
            json={"decision": "approved", "comment": ""},
        )

        viewer = await auth_headers(Role.VIEWER)
        resp = await client.get("/api/insights", headers=viewer)

        assert resp.status_code == 200
        titles = [i["title"] for i in resp.json()["items"]]
        assert insight.title in titles

    async def test_a_viewer_cannot_widen_the_filter_to_see_drafts(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        """A client-supplied filter must never make an authorisation decision."""
        insight = await make_insight(db, classification=Classification.INTERNAL)
        await submit(db, insight)
        headers = await auth_headers(Role.VIEWER)

        resp = await client.get("/api/insights?status=pending_approval", headers=headers)

        assert resp.status_code == 200
        assert resp.json()["items"] == []


class TestMemoryAndNotifications:
    async def test_memory_entries_are_created_and_retrievable(
        self, client: AsyncClient, db: AsyncSession, fake_embedder: Any, auth_headers: Any
    ) -> None:
        from app.services.memory_service import create_memory

        await create_memory(
            db,
            kind=MemoryKind.LESSON,
            title="Single-source foundry exposure was flagged and not acted on",
            content="The 2025 review identified the same concentration risk; no second source "
            "was qualified. Re-raising it without naming the blocker will not change the outcome.",
            tags=["supply-chain", "semiconductors"],
            source_ref={},
            classification=Classification.OFFICIAL,
            embedder=fake_embedder,
            created_by=None,
        )
        await db.commit()

        headers = await auth_headers(Role.ANALYST)
        listing = await client.get("/api/memory", headers=headers)

        assert listing.status_code == 200, listing.text
        entries = listing.json()
        assert len(entries) == 1
        assert entries[0]["kind"] == "lesson"

        search = await client.post(
            "/api/memory/search",
            headers=headers,
            json={"query": "foundry concentration exposure", "k": 5},
        )
        assert search.status_code == 200, search.text
        assert search.json()["hits"], "an embedded memory entry must be retrievable by search"

    async def test_memory_survives_without_an_embedder(self, db: AsyncSession) -> None:
        """Losing the memory would be worse than losing its searchability."""
        from app.services.memory_service import create_memory

        entry = await create_memory(
            db,
            kind=MemoryKind.DECISION,
            title="Recorded without an embedding provider",
            content="PENDING-CREDENTIALS mode must not silently discard institutional memory.",
            tags=[],
            source_ref={},
            classification=Classification.OFFICIAL,
            embedder=None,
            created_by=None,
        )
        await db.commit()

        stored = await db.get(MemoryEntry, entry.id)
        assert stored is not None
        assert stored.embedding is None

    async def test_submission_creates_a_notification_for_approvers(self, db: AsyncSession) -> None:
        insight = await make_insight(db)

        await submit(db, insight)

        notifications = list((await db.scalars(select(Notification))).all())
        assert notifications, "a pending approval must notify someone, or it will sit forever"
        assert any(n.kind is NotificationKind.APPROVAL_PENDING for n in notifications)
        assert any(n.role is Role.EXECUTIVE for n in notifications)

    async def test_notifications_are_listed_and_markable(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        insight = await make_insight(db)
        await submit(db, insight)
        headers = await auth_headers(Role.EXECUTIVE)

        listing = await client.get("/api/notifications", headers=headers)
        assert listing.status_code == 200, listing.text
        notes = listing.json()
        assert notes

        marked = await client.post("/api/notifications/read", headers=headers, json={"ids": []})
        assert marked.status_code == 200
        assert marked.json()["marked"] >= 1

        after = await client.get("/api/notifications", headers=headers)
        assert all(n["read_at"] is not None for n in after.json())


class TestBilingualBriefing:
    async def test_briefing_detail_carries_both_bodies(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        """Master prompt §12: 'Do not skip Arabic in briefings.'"""
        briefing = Briefing(
            id=uuid.uuid4(),
            date=datetime.now(UTC).date(),
            title="Daily Executive Briefing",
            body_en="## Executive summary\n\nSupply concentration is the dominant risk [1].",
            body_ar="## الملخص التنفيذي\n\nيمثل تركّز سلاسل التوريد الخطر الأبرز [1].",
            sections=[
                {
                    "key": "exec_summary",
                    "heading_en": "Executive summary",
                    "heading_ar": "الملخص التنفيذي",
                    "body_en": "Supply concentration is the dominant risk [1].",
                    "body_ar": "يمثل تركّز سلاسل التوريد الخطر الأبرز [1].",
                }
            ],
            citations={"items": [], "chunks": []},
            confidence=0.7,
            classification=Classification.INTERNAL,
            status=PublicationStatus.PUBLISHED,
        )
        db.add(briefing)
        await db.commit()

        headers = await auth_headers(Role.EXECUTIVE)
        resp = await client.get(f"/api/briefings/{briefing.id}", headers=headers)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["body_en"]
        assert body["body_ar"]
        assert any("؀" <= c <= "ۿ" for c in body["body_ar"]), (
            "body_ar must be genuine Arabic script"
        )
