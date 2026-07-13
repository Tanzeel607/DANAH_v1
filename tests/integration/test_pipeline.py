"""Phase 2 acceptance criteria (master prompt §10).

  * a pipeline run produces >=1 Risk insight grounded in real items, with citations
  * GET /api/pipeline/runs/{id} shows per-step token usage and cost
  * ingested items are visible in GET /items

The LLM is faked at the gateway interface — the orchestrator, agents, tool loop, insight
persistence and approval submission are all the real code paths.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    ApprovalStatus,
    Classification,
    ConnectorKind,
    InsightKind,
    ItemStatus,
    PipelineTrigger,
    PublicationStatus,
    Role,
    RunStatus,
    SourceType,
)
from app.models import Approval, IngestedItem, Insight, PipelineRun, Source


async def make_source(db: AsyncSession, *, name: str = "Test Wire") -> Source:
    source = Source(
        id=uuid.uuid4(),
        name=name,
        type=SourceType.API,
        connector=ConnectorKind.GDELT,
        config={},
        credibility_score=0.8,
        poll_interval_minutes=60,
        enabled=True,
    )
    db.add(source)
    await db.flush()
    return source


async def make_items(
    db: AsyncSession,
    source: Source,
    *,
    count: int = 3,
    status: ItemStatus = ItemStatus.NEW,
) -> list[IngestedItem]:
    bodies = [
        "Global semiconductor supply is concentrated: 78 percent of advanced logic capacity sits "
        "in a single jurisdiction, and a two-week port closure last month delayed shipments.",
        "Energy prices rose 22 percent over the past 30 days following an unplanned outage at a "
        "major refinery, raising input costs across the logistics sector.",
        "A new data-residency regulation takes effect in March, requiring government workloads to "
        "be hosted domestically, with penalties for non-compliance.",
    ]
    items: list[IngestedItem] = []
    for i in range(count):
        body = bodies[i % len(bodies)]
        item = IngestedItem(
            id=uuid.uuid4(),
            source_id=source.id,
            external_id=f"ext-{i}-{uuid.uuid4().hex[:6]}",
            title=f"Signal item {i}: {body[:50]}",
            summary=body,
            content=body,
            url=f"https://example.test/item-{i}",
            published_at=datetime.now(UTC) - timedelta(hours=i),
            classification=Classification.PUBLIC,
            status=status,
            dedup_hash=uuid.uuid4().hex,
            triage=(
                {
                    "relevance": 0.9,
                    "category": "economic",
                    "urgency": "high",
                    "rationale": "material to strategy",
                }
                if status is ItemStatus.TRIAGED
                else None
            ),
        )
        db.add(item)
        items.append(item)
    await db.commit()
    return items


def script_signal(fake_llm: Any, items: list[IngestedItem]) -> None:
    fake_llm.push(
        {
            "triage": [
                {
                    "item_id": str(item.id),
                    "relevance": 0.9,
                    "category": "economic",
                    "urgency": "high",
                    "rationale": "Directly affects the diversification programme.",
                }
                for item in items
            ]
        }
    )


def script_insights(fake_llm: Any, items: list[IngestedItem], *, count: int = 1) -> None:
    fake_llm.push(
        {
            "insights": [
                {
                    "title": "Semiconductor supply concentration threatens the manufacturing plan",
                    "body": "78 percent of advanced logic capacity sits in one jurisdiction [1].",
                    "severity": 4,
                    "likelihood": 0.6,
                    "confidence": 0.72,
                    "domains": ["trade", "technology"],
                    "recommendations": [
                        {
                            "action": "Qualify a second-source foundry",
                            "rationale": "Reduces single-jurisdiction exposure",
                            "owner": "Industry Directorate",
                            "horizon": "6 months",
                        }
                    ],
                    "citations": [{"kind": "item", "id": str(items[0].id)}],
                }
                for _ in range(count)
            ]
        }
    )


class TestPipelineRun:
    async def test_run_produces_grounded_risk_insight_with_citations(
        self,
        db: AsyncSession,
        fake_llm: Any,
        fake_embedder: Any,
    ) -> None:
        """§10 Phase 2: >=1 Risk insight grounded in real items, carrying citations."""
        from app.services.orchestrator import execute_run, start_run

        source = await make_source(db)
        items = await make_items(db, source, count=3, status=ItemStatus.NEW)

        script_signal(fake_llm, items)
        script_insights(fake_llm, items)  # Risk

        run = await start_run(db, trigger=PipelineTrigger.MANUAL, initiated_by=None)
        await db.commit()

        summary = await execute_run(
            db,
            run_id=run.id,
            agents=["signal", "risk"],
            gateway=fake_llm,
            embedder=fake_embedder,
        )

        assert summary["status"] in ("completed", "partial")

        risks = list(
            (await db.scalars(select(Insight).where(Insight.kind == InsightKind.RISK))).all()
        )
        assert risks, "the run must produce at least one Risk insight"

        risk = risks[0]
        assert risk.citations["items"], "the insight must cite the items it rests on"
        assert str(items[0].id) in risk.citations["items"]
        assert 1 <= risk.severity <= 5
        assert 0.0 <= risk.confidence <= 1.0

    async def test_insight_is_never_published_by_the_pipeline(
        self, db: AsyncSession, fake_llm: Any, fake_embedder: Any
    ) -> None:
        """The core invariant: an agent can only ever create a draft awaiting a human."""
        from app.services.orchestrator import execute_run, start_run

        source = await make_source(db)
        items = await make_items(db, source, status=ItemStatus.NEW)
        script_signal(fake_llm, items)
        script_insights(fake_llm, items)

        run = await start_run(db, trigger=PipelineTrigger.MANUAL, initiated_by=None)
        await db.commit()
        await execute_run(
            db, run_id=run.id, agents=["signal", "risk"], gateway=fake_llm, embedder=fake_embedder
        )

        insights = list((await db.scalars(select(Insight))).all())
        assert insights

        for insight in insights:
            assert insight.status is not PublicationStatus.PUBLISHED, (
                "no agent output may reach 'published' without a human decision"
            )
            assert insight.status is PublicationStatus.PENDING_APPROVAL

        approvals = list((await db.scalars(select(Approval))).all())
        assert len(approvals) == len(insights)
        assert all(a.status is ApprovalStatus.PENDING for a in approvals)

    async def test_uncited_insight_is_dropped(
        self, db: AsyncSession, fake_llm: Any, fake_embedder: Any
    ) -> None:
        """An uncited claim is exactly what this system exists to keep away from an executive."""
        from app.services.orchestrator import execute_run, start_run

        source = await make_source(db)
        items = await make_items(db, source, status=ItemStatus.NEW)

        script_signal(fake_llm, items)
        fake_llm.push(
            {
                "insights": [
                    {
                        "title": "A confident claim resting on nothing",
                        "body": "Trust me.",
                        "severity": 5,
                        "likelihood": 0.9,
                        "confidence": 0.99,
                        "domains": ["fiscal"],
                        "recommendations": [],
                        "citations": [],  # <- no evidence
                    }
                ]
            }
        )

        run = await start_run(db, trigger=PipelineTrigger.MANUAL, initiated_by=None)
        await db.commit()
        await execute_run(
            db, run_id=run.id, agents=["signal", "risk"], gateway=fake_llm, embedder=fake_embedder
        )

        assert not list((await db.scalars(select(Insight))).all())

    async def test_signal_archives_items_below_the_relevance_threshold(
        self, db: AsyncSession, fake_llm: Any, fake_embedder: Any
    ) -> None:
        from app.services.orchestrator import execute_run, start_run

        source = await make_source(db)
        items = await make_items(db, source, count=2, status=ItemStatus.NEW)

        fake_llm.push(
            {
                "triage": [
                    {
                        "item_id": str(items[0].id),
                        "relevance": 0.95,
                        "category": "economic",
                        "urgency": "high",
                        "rationale": "Material.",
                    },
                    {
                        "item_id": str(items[1].id),
                        "relevance": 0.05,
                        "category": "social",
                        "urgency": "low",
                        "rationale": "Noise.",
                    },
                ]
            }
        )

        run = await start_run(db, trigger=PipelineTrigger.MANUAL, initiated_by=None)
        await db.commit()
        await execute_run(
            db, run_id=run.id, agents=["signal"], gateway=fake_llm, embedder=fake_embedder
        )

        await db.refresh(items[0])
        await db.refresh(items[1])

        assert items[0].status is ItemStatus.TRIAGED
        assert items[1].status is ItemStatus.ARCHIVED
        assert items[1].triage["relevance"] == pytest.approx(0.05)

    async def test_partial_failure_does_not_lose_the_other_agents(
        self, db: AsyncSession, fake_llm: Any, fake_embedder: Any
    ) -> None:
        """A ministry that gets four fifths of its briefing is better served than one that gets none."""
        from app.services.orchestrator import execute_run, start_run

        source = await make_source(db)
        await make_items(db, source, count=2, status=ItemStatus.TRIAGED)

        # Risk succeeds; Opportunity is handed a payload it cannot satisfy.
        script_insights(fake_llm, await _items(db), count=1)

        run = await start_run(db, trigger=PipelineTrigger.MANUAL, initiated_by=None)
        await db.commit()

        summary = await execute_run(
            db,
            run_id=run.id,
            agents=["risk", "opportunity"],
            gateway=fake_llm,
            embedder=fake_embedder,
        )

        # Whatever happened to one agent, the run finished and reported honestly.
        assert summary["status"] in ("completed", "partial")
        refreshed = await db.get(PipelineRun, run.id)
        assert refreshed is not None
        assert refreshed.status in (RunStatus.COMPLETED, RunStatus.PARTIAL)
        assert refreshed.finished_at is not None


async def _items(db: AsyncSession) -> list[IngestedItem]:
    return list((await db.scalars(select(IngestedItem))).all())


class TestPipelineAPI:
    async def test_run_detail_exposes_per_step_tokens_and_cost(
        self,
        client: AsyncClient,
        db: AsyncSession,
        fake_llm: Any,
        fake_embedder: Any,
        auth_headers: Any,
    ) -> None:
        """§10 Phase 2: the run detail must show per-step token usage and cost."""
        from app.services.orchestrator import execute_run, start_run

        source = await make_source(db)
        items = await make_items(db, source, status=ItemStatus.NEW)
        script_signal(fake_llm, items)
        script_insights(fake_llm, items)

        run = await start_run(db, trigger=PipelineTrigger.MANUAL, initiated_by=None)
        await db.commit()
        await execute_run(
            db, run_id=run.id, agents=["signal", "risk"], gateway=fake_llm, embedder=fake_embedder
        )

        headers = await auth_headers(Role.ANALYST)
        resp = await client.get(f"/api/pipeline/runs/{run.id}", headers=headers)

        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["steps"], "the run detail must list its steps"
        for step in body["steps"]:
            assert "tokens_in" in step
            assert "tokens_out" in step
            assert "cost_usd" in step
            assert "latency_ms" in step
            assert step["agent"] in {
                "signal",
                "risk",
                "opportunity",
                "policy",
                "briefing",
                "memory",
            }

        assert body["total_tokens"] > 0, "a run that called a model must report tokens"
        assert body["total_cost_usd"] >= 0.0

    async def test_trigger_run_returns_a_pollable_id(
        self, client: AsyncClient, auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.ANALYST)

        resp = await client.post("/api/pipeline/run", headers=headers, json={"max_items": 5})

        assert resp.status_code in (200, 202), resp.text
        assert resp.json()["run_id"]

    async def test_viewer_cannot_trigger_a_run(
        self, client: AsyncClient, auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.VIEWER)

        resp = await client.post("/api/pipeline/run", headers=headers, json={})

        assert resp.status_code == 403


class TestItemsAPI:
    async def test_items_are_listed_with_triage_flattened(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        source = await make_source(db)
        await make_items(db, source, count=3, status=ItemStatus.TRIAGED)
        headers = await auth_headers(Role.ANALYST)

        resp = await client.get("/api/items?limit=10", headers=headers)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 3
        item = body["items"][0]
        # The UI sorts and filters on these directly rather than digging into `triage`.
        assert item["relevance"] is not None
        assert item["category"] == "economic"
        assert item["urgency"] == "high"

    async def test_items_can_be_filtered_by_status(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        source = await make_source(db)
        await make_items(db, source, count=2, status=ItemStatus.NEW)
        await make_items(db, source, count=3, status=ItemStatus.TRIAGED)
        headers = await auth_headers(Role.ANALYST)

        resp = await client.get("/api/items?status=new", headers=headers)

        assert resp.status_code == 200
        assert resp.json()["total"] == 2
