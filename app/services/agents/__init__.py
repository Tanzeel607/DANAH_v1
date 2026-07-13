"""The six DANAH agents (master prompt §7.3, architecture §4).

| Agent       | Tier    | Tools                                                   | Output           |
|-------------|---------|---------------------------------------------------------|------------------|
| Signal      | fast    | —                                                       | SignalOutput     |
| Risk        | primary | search_knowledge_base, search_ingested_items, get_memory | RiskOutput       |
| Opportunity | primary | search_knowledge_base, search_ingested_items, get_memory | OpportunityOutput|
| Policy      | primary | search_knowledge_base, search_ingested_items             | PolicyOutput     |
| Briefing    | primary | get_kpi_snapshot, get_memory                            | BriefingOutput   |
| Memory      | fast    | save_memory, get_memory                                 | MemoryOutput     |

Each is a `BaseAgent` configured with a versioned prompt file, a fixed tool list and a typed output
schema. The tool list is fixed at construction, which is what makes prompt injection survivable:
no instruction embedded in an ingested article can reach a tool the agent was not built with.

---

## The orchestrator's hand-off: `AgentContext.payload`

Agents read their inputs from `context.payload` and never query the database directly — the
orchestrator loads once (with the run's clearance applied in SQL, via
`tools.recent_items_for_analysis`) and hands the same evidence to the agents that fan out in
parallel. These are the keys each agent reads; every one is optional, and an agent handed nothing
returns an empty, honest answer rather than failing its step.

| Key               | Type              | Read by                  | Produced by                          |
|-------------------|-------------------|--------------------------|--------------------------------------|
| `items`           | `list[dict]`      | Signal, Risk, Opp, Policy| `tools.recent_items_for_analysis()`  |
| `item_ids`        | `list[UUID]`      | `BaseAgent.summarise_input` | the orchestrator                  |
| `existing_titles` | `list[str]`       | Risk, Opportunity        | `tools.existing_insight_titles()`    |
| `insights`        | `list[dict]`      | Briefing, Memory         | the Risk/Opportunity/Policy outputs  |
| `briefing_date`   | `str` (ISO date)  | Briefing                 | the orchestrator (falls back to `TZ`)|
| `briefing`        | `dict`            | Memory                   | the Briefing output                  |
| `run_summary`     | `dict`            | Memory                   | the orchestrator                     |

An `insights` entry carries `kind`, `title`, `body`, `severity`, `likelihood`, `confidence`,
`domains`, `recommendations` and `citations`. `citations` may be either the agent-side
`[{"kind": "item"|"chunk", "id": ...}]` list or the persisted
`{"items": [...], "chunks": [...]}` column shape — the Briefing agent accepts both.

## Two write-path contracts worth knowing before you wire the orchestrator

* **`MemoryAgent` returns entries; it does not save them.** The pipeline persists
  `MemoryOutput.entries` itself (`memory_service.create_memory`, which embeds and classifies them).
  The agent holds the `save_memory` tool for the ad-hoc path only, and its run-mode user message
  forbids calling it — an entry both saved and returned would be written twice.
* **`BriefingAgent.render_arabic()` is a second, separate call.** Run it after `run()` succeeds. It
  returns `None` — never raises — when the rendering fails or comes back structurally unfaithful.
  Save the English briefing and mark the run `partial`; losing the Arabic column is much cheaper
  than losing the briefing.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.enums import AgentName
from app.exceptions import OrchestrationError
from app.services.agents.base import (
    PROMPTS_DIR,
    AgentContext,
    AgentOutput,
    BaseAgent,
    evidence_block,
)
from app.services.agents.briefing_agent import BriefingAgent
from app.services.agents.memory_agent import MemoryAgent
from app.services.agents.opportunity_agent import OpportunityAgent
from app.services.agents.policy_agent import PolicyAgent
from app.services.agents.risk_agent import RiskAgent
from app.services.agents.signal_agent import SignalAgent
from app.services.agents.tools import (
    TOOL_REGISTRY,
    ToolContext,
    existing_insight_titles,
    item_uuids,
    recent_items_for_analysis,
)

#: Every agent, by the name persisted in `pipeline_steps.agent`. The parameter is `Any` because the
#: registry is heterogeneous by design — each agent is generic over a *different* output schema, and
#: the caller recovers the concrete type by construction, not by lookup.
AGENT_REGISTRY: dict[AgentName, type[BaseAgent[Any]]] = {
    AgentName.SIGNAL: SignalAgent,
    AgentName.RISK: RiskAgent,
    AgentName.OPPORTUNITY: OpportunityAgent,
    AgentName.POLICY: PolicyAgent,
    AgentName.BRIEFING: BriefingAgent,
    AgentName.MEMORY: MemoryAgent,
}


def build_agent(name: AgentName, settings: Settings | None = None) -> BaseAgent[Any]:
    """Construct an agent by name — for the pipeline-replay and admin paths that hold only a name."""
    agent_class = AGENT_REGISTRY.get(name)
    if agent_class is None:  # pragma: no cover - unreachable while AgentName and the registry agree
        raise OrchestrationError(
            f"No agent is registered under the name {name.value!r}.",
            detail={"known": sorted(a.value for a in AGENT_REGISTRY)},
        )
    return agent_class(settings)


__all__ = [
    "AGENT_REGISTRY",
    "PROMPTS_DIR",
    "TOOL_REGISTRY",
    "AgentContext",
    "AgentOutput",
    "BaseAgent",
    "BriefingAgent",
    "MemoryAgent",
    "OpportunityAgent",
    "PolicyAgent",
    "RiskAgent",
    "SignalAgent",
    "ToolContext",
    "build_agent",
    "evidence_block",
    "existing_insight_titles",
    "item_uuids",
    "recent_items_for_analysis",
]
