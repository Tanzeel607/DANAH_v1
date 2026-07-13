"""Tools the agents may call (master prompt §7.3).

Plain async functions with JSON schemas. Each agent is given a fixed, explicit tool list at
construction — an instruction embedded in an ingested news article can never grant an agent a
tool it was not built with.

**Every tool that reads data takes the caller's clearance and applies it in SQL.** An agent
running on behalf of a scheduled pipeline gets the pipeline's clearance, not unrestricted access;
the same filter that protects a viewer in chat protects the corpus here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.enums import Classification, ItemStatus, MemoryKind, classification_at_or_below
from app.models import IngestedItem, Insight, MemoryEntry, Source

log = structlog.get_logger(__name__)

MAX_TOOL_RESULT_CHARS = 6000


@dataclass(slots=True)
class ToolContext:
    session: AsyncSession
    clearance: Classification = Classification.OFFICIAL_SENSITIVE
    embedder: Any = None
    settings: Settings = field(default_factory=get_settings)


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[str]]

    def schema(self) -> dict[str, Any]:
        """Vendor-neutral shape. Both providers translate from this (see the provider clients)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


# ---------------------------------------------------------------------------
# search_knowledge_base
# ---------------------------------------------------------------------------
async def _search_knowledge_base(args: dict[str, Any], ctx: ToolContext) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: 'query' is required."

    if ctx.embedder is None:
        return "The knowledge base is unavailable (no embedding provider configured)."

    from app.services.rag.retriever import Retriever

    retriever = Retriever(ctx.embedder, ctx.settings)
    hits = await retriever.retrieve(
        ctx.session,
        query,
        k=int(args.get("k", 5)),
        classification_ceiling=ctx.clearance,
    )
    if not hits:
        return "No documents in the knowledge base match that query."

    return _truncate(
        "\n\n".join(
            f"[chunk:{h.chunk_id}] {h.document_title} (relevance {h.score:.2f})\n{h.snippet(500)}"
            for h in hits
        )
    )


# ---------------------------------------------------------------------------
# search_ingested_items
# ---------------------------------------------------------------------------
async def _search_ingested_items(args: dict[str, Any], ctx: ToolContext) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: 'query' is required."

    allowed = classification_at_or_below(ctx.clearance)
    tsquery = func.websearch_to_tsquery("simple", query)

    stmt = (
        select(IngestedItem, Source.name, Source.credibility_score)
        .join(Source, Source.id == IngestedItem.source_id)
        .where(
            IngestedItem.content_tsv.op("@@")(tsquery),
            IngestedItem.classification.in_(allowed),
        )
        .order_by(func.ts_rank(IngestedItem.content_tsv, tsquery).desc())
        .limit(min(int(args.get("k", 8)), 20))
    )

    days = args.get("within_days")
    if days:
        stmt = stmt.where(
            IngestedItem.published_at >= datetime.now(UTC) - timedelta(days=int(days))
        )
    category = args.get("category")
    if category:
        stmt = stmt.where(IngestedItem.triage["category"].astext == str(category))

    rows = (await ctx.session.execute(stmt)).all()
    if not rows:
        return "No ingested items match that query."

    return _truncate(
        "\n\n".join(
            f"[item:{item.id}] {item.title}\n"
            f"    source: {source_name} (credibility {credibility:.2f})\n"
            f"    published: {item.published_at.isoformat() if item.published_at else 'unknown'}\n"
            f"    {(item.summary or item.content or '')[:400]}"
            for item, source_name, credibility in rows
        )
    )


# ---------------------------------------------------------------------------
# get_memory / save_memory
# ---------------------------------------------------------------------------
async def _get_memory(args: dict[str, Any], ctx: ToolContext) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: 'query' is required."

    from app.services.memory_service import search_memory

    hits = await search_memory(
        ctx.session,
        query=query,
        k=int(args.get("k", 5)),
        clearance=ctx.clearance,
        embedder=ctx.embedder,
    )
    if not hits:
        return "Institutional memory holds nothing relevant to that query."

    return _truncate(
        "\n\n".join(
            f"[memory:{entry.id}] ({entry.kind.value}) {entry.title}\n{entry.content[:500]}"
            for entry, _score in hits
        )
    )


async def _save_memory(args: dict[str, Any], ctx: ToolContext) -> str:
    title = str(args.get("title", "")).strip()
    content = str(args.get("content", "")).strip()
    if not title or not content:
        return "Error: both 'title' and 'content' are required."

    from app.services.memory_service import create_memory

    try:
        kind = MemoryKind(str(args.get("kind", "lesson")))
    except ValueError:
        kind = MemoryKind.LESSON

    entry = await create_memory(
        ctx.session,
        kind=kind,
        title=title[:500],
        content=content[:20000],
        tags=[str(t)[:100] for t in args.get("tags", [])][:10],
        source_ref=args.get("source_ref") or {},
        classification=ctx.clearance,
        embedder=ctx.embedder,
        created_by=None,
    )
    return f"Saved memory entry {entry.id} ({kind.value}): {entry.title}"


# ---------------------------------------------------------------------------
# get_kpi_snapshot
# ---------------------------------------------------------------------------
async def _get_kpi_snapshot(args: dict[str, Any], ctx: ToolContext) -> str:
    """The same figures the dashboard shows — so the briefing and the UI cannot disagree."""
    from app.services.dashboard_service import kpi_snapshot

    snapshot = await kpi_snapshot(ctx.session, clearance=ctx.clearance)
    return json.dumps(snapshot.model_dump(mode="json"), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
TOOL_REGISTRY: dict[str, Tool] = {
    "search_knowledge_base": Tool(
        name="search_knowledge_base",
        description=(
            "Search the ministry's own document corpus (strategies, policies, frameworks) for "
            "passages relevant to a query. Use this to ground an analysis in official policy "
            "rather than in your own assumptions. Returns numbered chunks you can cite by id."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look for, in natural language.",
                },
                "k": {
                    "type": "integer",
                    "description": "How many passages to return (1-10).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        handler=_search_knowledge_base,
    ),
    "search_ingested_items": Tool(
        name="search_ingested_items",
        description=(
            "Search the signal items ingested from external sources (economic indicators, news, "
            "humanitarian reports). Use this to find corroborating or contradicting evidence for "
            "a claim. Returns items you can cite by id."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms."},
                "k": {"type": "integer", "description": "How many items (1-20).", "default": 8},
                "within_days": {
                    "type": "integer",
                    "description": "Only items published within this many days.",
                },
                "category": {
                    "type": "string",
                    "enum": ["economic", "geopolitical", "regulatory", "technology", "social"],
                    "description": "Restrict to one triage category.",
                },
            },
            "required": ["query"],
        },
        handler=_search_ingested_items,
    ),
    "get_memory": Tool(
        name="get_memory",
        description=(
            "Search institutional memory: past decisions, lessons learned, and standing context. "
            "Use this before recommending an action, so you do not re-propose something the "
            "ministry already tried, rejected, or learned from."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to recall."},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        handler=_get_memory,
    ),
    "save_memory": Tool(
        name="save_memory",
        description=(
            "Record something durable enough to be worth remembering across runs: a decision "
            "taken, a lesson learned, or standing context. Do NOT save routine observations or "
            "restatements of an item — memory that records everything recalls nothing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["decision", "lesson", "context"],
                    "description": "decision = a choice made; lesson = something learned; "
                    "context = durable background.",
                },
                "title": {"type": "string", "description": "One line, specific."},
                "content": {
                    "type": "string",
                    "description": "The substance, and why it matters later.",
                },
                "tags": {"type": "array", "items": {"type": "string"}},
                "source_ref": {
                    "type": "object",
                    "description": "Ids this came from: {run_id, insight_ids, item_ids}.",
                },
            },
            "required": ["kind", "title", "content"],
        },
        handler=_save_memory,
    ),
    "get_kpi_snapshot": Tool(
        name="get_kpi_snapshot",
        description=(
            "Current headline figures: items in the last 24h, high-urgency count, average "
            "insight confidence, most active domains, active sources. Use this to open a "
            "briefing with facts rather than atmosphere."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_get_kpi_snapshot,
    ),
}


async def execute_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> str:
    """Run a tool by name. A tool never raises into the agent loop.

    A failing tool returns an error *string*, which the model can read and route around. Raising
    would abort the whole agent step over a single bad search — losing the analysis along with it.
    """
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        log.warning("agent_called_unknown_tool", tool=name)
        return f"Error: no tool named '{name}'. Available: {', '.join(sorted(TOOL_REGISTRY))}."

    try:
        result = await tool.handler(arguments, ctx)
    except Exception as exc:
        log.warning("agent_tool_failed", tool=name, error_type=type(exc).__name__, error=str(exc))
        return f"Error: the '{name}' tool failed ({type(exc).__name__}). Continue without it."

    log.info("agent_tool_used", tool=name, result_chars=len(result))
    return result


def _truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS].rstrip() + "\n\n[…truncated: refine your query]"


async def recent_items_for_analysis(
    session: AsyncSession,
    *,
    clearance: Classification,
    statuses: tuple[ItemStatus, ...] = (ItemStatus.TRIAGED,),
    limit: int = 40,
    min_relevance: float | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Load items for an agent's evidence block — the orchestrator's hand-off to Risk/Opp/Policy."""
    allowed = classification_at_or_below(clearance)

    stmt = (
        select(IngestedItem, Source.name, Source.credibility_score)
        .join(Source, Source.id == IngestedItem.source_id)
        .where(
            IngestedItem.status.in_(statuses),
            IngestedItem.classification.in_(allowed),
        )
        .order_by(IngestedItem.published_at.desc().nullslast(), IngestedItem.created_at.desc())
        .limit(limit)
    )
    if category:
        stmt = stmt.where(IngestedItem.triage["category"].astext == category)

    rows = (await session.execute(stmt)).all()

    items: list[dict[str, Any]] = []
    for item, source_name, credibility in rows:
        relevance = item.relevance
        if min_relevance is not None and (relevance is None or relevance < min_relevance):
            continue
        items.append(
            {
                "id": str(item.id),
                "title": item.title,
                "summary": item.summary,
                "content": (item.content or "")[:1200] or None,
                "url": item.url,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "source_name": source_name,
                "credibility": round(float(credibility), 2),
                "category": item.category,
                "urgency": item.urgency,
                "relevance": relevance,
            }
        )
    return items


async def existing_insight_titles(session: AsyncSession, *, days: int = 14) -> list[str]:
    """Recent insight titles, so an agent can be told not to re-raise the same finding daily."""
    since = datetime.now(UTC) - timedelta(days=days)
    titles = await session.scalars(
        select(Insight.title)
        .where(Insight.created_at >= since)
        .order_by(Insight.created_at.desc())
        .limit(40)
    )
    return list(titles.all())


async def memory_count(session: AsyncSession) -> int:
    total = await session.scalar(select(func.count(MemoryEntry.id)))
    return int(total or 0)


def item_uuids(items: list[dict[str, Any]]) -> list[uuid.UUID]:
    return [uuid.UUID(i["id"]) for i in items]
