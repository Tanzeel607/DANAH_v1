"""Three short public-domain sample documents, indexed at seed time so chat works immediately.

Fictional ministry strategy material — no real government content. They exist so that a fresh
`docker compose up && make seed` can answer a grounded question with a real citation, which is
the Phase-1 acceptance criterion, without the operator first having to find a PDF.
"""

from __future__ import annotations

SAMPLE_DOCUMENTS: list[dict[str, str]] = [
    {
        "filename": "national-strategy-2030.md",
        "title": "National Economic Diversification Strategy 2030",
        "classification": "INTERNAL",
        "content": """\
# National Economic Diversification Strategy 2030

## Purpose

This strategy sets the Ministry's direction for reducing the national economy's dependence on
hydrocarbon revenue over the period 2026-2030. It is the reference document for all sectoral
plans and investment decisions taken by the Ministry during that period.

## Strategic Priorities

### Priority 1 — Non-oil GDP growth

The Ministry targets a non-oil share of GDP of 65 percent by 2030, up from 51 percent in 2025.
Growth is to be concentrated in four sectors: advanced manufacturing, logistics and re-export,
financial services, and tourism. Each sector has a designated lead directorate and an annual
contribution target reviewed every quarter.

### Priority 2 — Sovereign data and digital infrastructure

All government workloads classified OFFICIAL-SENSITIVE or above must be hosted within national
borders by the end of 2027. Cloud services procured after January 2026 must demonstrate data
residency compliance as a condition of award. The Ministry will publish a register of approved
sovereign hosting providers.

### Priority 3 — Public sector capability

Forty thousand civil servants are to be trained in data literacy and digital service design by
the end of 2027. Training is delivered through the National Institute of Administration, with
completion tracked as a ministerial KPI.

## Fiscal Framework

The strategy is funded from the Diversification Reserve, capped at 4 percent of annual budgeted
expenditure. Programmes exceeding a five-year payback period require Cabinet approval. The
Ministry does not fund operating subsidies from the Reserve under any circumstances.

## Risk Appetite

The Ministry accepts elevated execution risk in the advanced manufacturing and tourism sectors,
where returns are long-dated. It does not accept material risk to fiscal stability, data
sovereignty, or the continuity of essential public services.
""",
    },
    {
        "filename": "trade-policy-framework.md",
        "title": "Trade Policy and Supply Chain Resilience Framework",
        "classification": "INTERNAL",
        "content": """\
# Trade Policy and Supply Chain Resilience Framework

## Scope

This framework governs the Ministry's response to trade disruption, tariff changes, and supply
chain concentration risk. It applies to all imported goods designated as strategic inputs.

## Strategic Input Categories

The Ministry designates five categories as strategic inputs: pharmaceutical precursors, staple
foodstuffs (wheat, rice, cooking oil), semiconductors and electronic components, industrial
fertiliser, and critical minerals used in energy storage.

## Concentration Thresholds

Where more than 60 percent of a strategic input is sourced from a single country of origin, the
Ministry treats the position as a concentration risk requiring an active mitigation plan. Where
the figure exceeds 80 percent, the mitigation plan must be reviewed by the Under-Secretary
each quarter until the share falls below the threshold.

## Response Instruments

The Ministry may respond to trade disruption using four instruments, in escalating order:
diversification of suppliers; strategic reserve release; tariff adjustment within World Trade
Organization commitments; and, as a measure of last resort, temporary import substitution
incentives. Export restrictions are not an instrument available to the Ministry.

## Monitoring

Trade flows for strategic inputs are monitored monthly. Energy price movements above 15 percent
in a rolling 30-day window trigger an immediate review of the logistics cost assumptions
underpinning the diversification programme.

## Sanctions Exposure

Any counterparty appearing on a sanctions list of a jurisdiction in which the Ministry holds
reserves must be reported to the Compliance Directorate within two working days.
""",
    },
    {
        "filename": "ai-governance-policy.md",
        "title": "Policy on the Governance of Artificial Intelligence Systems",
        "classification": "OFFICIAL",
        "content": """\
# Policy on the Governance of Artificial Intelligence Systems

## Applicability

This policy applies to every artificial intelligence system procured, developed, or operated by
the Ministry, including systems that generate analysis for internal decision support.

## Core Requirements

### Human accountability

No output produced by an AI system may be published, acted upon, or presented as a Ministry
position without an accountable human officer approving it. The approving officer is recorded and
remains accountable for the decision. Automation does not transfer accountability.

### Grounding and citation

An AI system used for analysis must cite the evidence supporting each factual claim it makes, or
state explicitly that the evidence is unavailable. A system that produces confident, uncited
assertions is not fit for ministerial use.

### Auditability

Every AI-generated output, and every human decision on it, must be recorded in a tamper-evident
audit trail retained for a minimum of seven years.

### Classification handling

An AI system must not process data above the clearance of the officer invoking it. Access control
is to be enforced at the data layer, not by instructing the model.

## Prohibited Uses

AI systems must not be used to make final determinations on individual entitlements, employment
decisions, or any matter carrying a legal consequence for a named person, without a documented
human review of the individual case.

## Review

This policy is reviewed annually, or sooner if a material incident occurs. The Chief Data Officer
owns the review.
""",
    },
]
