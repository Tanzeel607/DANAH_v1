# DANAH Strategic Intelligence Platform — Master Build Prompt for Claude Code

> **How to use this file:** Attach this file, `DANAH_DEVELOPER_ARCHITECTURE.md`, `.env.example`, and the existing `DANAH_Strategic_Intelligence_Platform_v11.html` prototype to Claude Code, then instruct it to read all files and implement this specification phase by phase. This document is the single source of truth for WHAT to build. The architecture document explains WHY and HOW the pieces fit together.

---

## 1. Your Role and Mission

You are the lead engineer building **DANAH**, an enterprise-grade Strategic Intelligence Platform backend for a government ministry. An HTML front-end prototype (v11) already exists. It is a high-fidelity simulation: the UI is complete, but every intelligent behavior is faked with timers, scripted animations, and keyword matching.

Your mission: **build the real production system behind that UI.** That means a real backend with a real LLM-powered agent system, real retrieval (RAG), real data ingestion, persistent storage, and server-side security. The prototype's simulated components are labeled with production hooks; you are implementing those hooks.

Build this as if it will handle OFFICIAL-SENSITIVE government data, because eventually it will.

---

## 2. Project Context

**What DANAH does:** It continuously watches external data sources (economic indicators, news, policy announcements), routes incoming items through a pipeline of specialized AI agents (Signal → Risk → Opportunity → Policy → Executive Briefing), stores institutional memory, and lets executives query everything through a grounded chat assistant that answers with citations and confidence scores. All AI outputs pass through a human approval queue before publication. The platform is bilingual (English and Arabic).

**What exists:** A single-file HTML prototype with the complete UI, a documented API contract, and honest labels on every simulated component.

**What you are building:** The backend service (API + agents + ingestion + RAG + security) that the HTML front end will later be wired to. Do NOT rebuild the front end. Design every endpoint so the existing HTML file can consume it with minimal changes (JSON over REST, CORS enabled for local file / configured origins).

---

## 3. Non-Negotiable Engineering Standards

1. **Language and typing:** Python 3.12+, full type hints everywhere, `mypy --strict` must pass.
2. **Framework:** FastAPI with Pydantic v2 models for every request/response. No untyped dicts crossing API boundaries.
3. **Async first:** All I/O (DB, HTTP, LLM calls) is async. Use `httpx.AsyncClient` and `asyncpg` via SQLAlchemy 2.0 async.
4. **12-factor config:** ALL configuration via environment variables loaded through a single `pydantic-settings` `Settings` class. Never hardcode secrets, URLs, model names, or magic numbers. The provided `.env.example` is the contract — every variable in it must be read by `Settings`, and every setting in `Settings` must appear in `.env.example`.
5. **Migrations:** Alembic for all schema changes. Never `create_all` in production paths.
6. **Error handling:** Custom exception hierarchy (`DanahError` base → `AuthError`, `LLMGatewayError`, `IngestionError`, `RetrievalError`, `OrchestrationError`, `ApprovalError`). Global exception handlers return structured JSON errors: `{"error": {"code": str, "message": str, "request_id": str}}`. Never leak stack traces to clients.
7. **Logging:** Structured JSON logging (`structlog`) with request IDs propagated through every layer, including into LLM call logs. Log level from env.
8. **Testing:** `pytest` + `pytest-asyncio`. Minimum coverage: every service module has unit tests; every endpoint has at least one integration test using `httpx.AsyncClient` against the app with a test database. LLM calls are mocked in tests via the gateway interface.
9. **Linting/formatting:** `ruff` (lint + format). Zero warnings on CI.
10. **Security:** Passwords hashed with `argon2`. JWTs signed with secret from env (HS256 now, key-rotation-ready design). All queries parameterized (SQLAlchemy). Input validation via Pydantic. Rate limiting middleware on auth and chat endpoints. RBAC enforced in the API layer via dependency injection, never in the client.
11. **Docs:** OpenAPI auto-docs must be accurate (response models, auth requirements, examples). Write a `README.md` covering setup, run, test, and architecture summary. Write `docs/RUNBOOK.md` for operations.
12. **Docker:** `docker-compose.yml` bringing up: api, worker, scheduler, postgres (with pgvector), redis. One command to run: `docker compose up`. Also support bare-metal dev via `make dev`.
13. **Git hygiene:** Conventional commits. Commit at every meaningful milestone. `.gitignore` must exclude `.env`, caches, and volumes.
14. **No placeholder code in delivered phases.** A phase is done only when its acceptance criteria (Section 10) pass. `TODO` comments are allowed only for explicitly out-of-scope future phases and must reference the phase number.

---

## 4. Technology Stack (Locked Decisions — do not substitute)

| Concern | Choice | Notes |
|---|---|---|
| API framework | FastAPI (Python 3.12) | async, OpenAPI out of the box |
| ORM / DB | SQLAlchemy 2.0 async + PostgreSQL 16 | single primary database |
| Vector store | **pgvector** extension in the same PostgreSQL | sovereignty-friendly; no external vector SaaS |
| Cache / queue broker | Redis 7 | cache, rate limits, task broker |
| Background jobs | **ARQ** (async Redis queue) | ingestion syncs, pipeline runs, embeddings |
| Scheduler | ARQ cron jobs | periodic source polling + daily pipeline |
| LLM providers | Anthropic Claude (primary), OpenAI (optional fallback) | behind a provider-agnostic gateway |
| Embeddings | Voyage AI (primary, `voyage-3.5`) or OpenAI (`text-embedding-3-small`) | selected via env |
| Auth | JWT access + refresh tokens, argon2 hashing | OIDC/SSO hook stub for Phase 4 |
| Migrations | Alembic | |
| HTTP client | httpx (async) | all connectors + LLM calls |
| Observability | structlog JSON logs + `/metrics` (Prometheus format via `prometheus-fastapi-instrumentator`) | |
| Container | Docker + docker-compose | |

If a library above is unavailable, choose the closest actively-maintained equivalent and record the decision in `docs/DECISIONS.md`.

---

## 5. Repository Structure (create exactly this)

```
danah/
├── README.md
├── .env.example                  # provided — copy to repo root
├── .gitignore
├── docker-compose.yml
├── Dockerfile
├── Makefile                      # dev, test, lint, migrate, seed targets
├── pyproject.toml
├── alembic/
│   └── versions/
├── app/
│   ├── main.py                   # FastAPI app factory, middleware, routers
│   ├── config.py                 # pydantic-settings Settings (mirrors .env.example)
│   ├── deps.py                   # DI: db session, current_user, require_role
│   ├── exceptions.py
│   ├── logging.py
│   ├── api/
│   │   ├── auth.py
│   │   ├── chat.py
│   │   ├── knowledge.py
│   │   ├── sources.py
│   │   ├── items.py
│   │   ├── pipeline.py
│   │   ├── insights.py           # risks, opportunities, policy
│   │   ├── briefings.py
│   │   ├── approvals.py
│   │   ├── dashboard.py
│   │   ├── memory.py
│   │   ├── audit.py
│   │   └── admin.py
│   ├── models/                   # SQLAlchemy models (one file per aggregate)
│   ├── schemas/                  # Pydantic request/response schemas
│   ├── services/
│   │   ├── auth_service.py
│   │   ├── rag/
│   │   │   ├── chunking.py
│   │   │   ├── embeddings.py
│   │   │   ├── retriever.py
│   │   │   └── indexer.py
│   │   ├── llm/
│   │   │   ├── gateway.py        # provider-agnostic interface
│   │   │   ├── anthropic_provider.py
│   │   │   ├── openai_provider.py
│   │   │   └── usage_tracker.py
│   │   ├── agents/
│   │   │   ├── base.py           # BaseAgent: prompt assembly, tool loop, output schema
│   │   │   ├── signal_agent.py
│   │   │   ├── risk_agent.py
│   │   │   ├── opportunity_agent.py
│   │   │   ├── policy_agent.py
│   │   │   ├── briefing_agent.py
│   │   │   ├── memory_agent.py
│   │   │   └── prompts/          # versioned system prompts, EN + AR guidance
│   │   ├── orchestrator.py       # pipeline runner
│   │   ├── ingestion/
│   │   │   ├── base_connector.py
│   │   │   ├── worldbank.py
│   │   │   ├── gdelt.py
│   │   │   ├── rss.py
│   │   │   └── reliefweb.py
│   │   ├── approval_service.py
│   │   ├── memory_service.py
│   │   ├── audit_service.py      # hash-chained audit writes
│   │   └── notification_service.py
│   ├── workers/
│   │   ├── worker.py             # ARQ worker settings + task registry
│   │   └── tasks.py              # sync_source, run_pipeline, embed_document, daily_brief
│   └── security/
│       ├── jwt.py
│       ├── rbac.py               # role + classification enforcement
│       └── rate_limit.py
├── scripts/
│   ├── seed.py                   # seed roles, admin user, default sources, sample docs
│   └── create_admin.py
├── tests/
│   ├── conftest.py               # test DB, app fixture, fake LLM gateway
│   ├── unit/
│   └── integration/
└── docs/
    ├── RUNBOOK.md
    ├── DECISIONS.md
    └── API.md                    # human-readable endpoint reference
```

---

## 6. Database Schema (implement via Alembic migration 0001)

Use UUID primary keys (`uuid7` if available, else `uuid4`), `created_at`/`updated_at` timestamps on every table. Classification is an enum: `PUBLIC | INTERNAL | OFFICIAL | OFFICIAL_SENSITIVE`. Language fields are `en | ar`.

**users**: id, email (unique), full_name, password_hash, role (`admin | executive | analyst | viewer`), is_active, last_login_at
**refresh_tokens**: id, user_id FK, token_hash, expires_at, revoked_at
**documents**: id, title, filename, mime_type, storage_path, language, classification, status (`pending | processing | indexed | failed`), uploaded_by FK, chunk_count, error
**document_chunks**: id, document_id FK, chunk_index, content (text), token_count, embedding `vector(EMBEDDING_DIM)`, metadata jsonb — with an HNSW index on embedding
**sources**: id, name, type (`api | rss | webhook | manual`), connector (`worldbank | gdelt | rss | reliefweb | custom`), config jsonb, credibility_score (0–1), poll_interval_minutes, enabled, last_synced_at, last_status
**ingested_items**: id, source_id FK, external_id, title, summary, content, url, published_at, language, raw jsonb, dedup_hash (unique), triage jsonb (filled by Signal Agent: relevance, category, urgency), status (`new | triaged | analyzed | archived`)
**pipeline_runs**: id, trigger (`manual | scheduled`), status (`running | completed | failed | partial`), started_at, finished_at, stats jsonb, initiated_by FK nullable
**pipeline_steps**: id, run_id FK, agent (enum of 6 agents), status, input_ref jsonb, output_ref jsonb, tokens_in, tokens_out, cost_usd, latency_ms, error
**insights**: id, kind (`risk | opportunity | policy`), title, body, severity/impact (1–5), likelihood (0–1 nullable), confidence (0–1), domains text[], recommendations jsonb, citations jsonb (chunk ids + source item ids), language, classification, status (`draft | pending_approval | published | rejected`), run_id FK nullable, created_by_agent
**briefings**: id, date, title, body_en, body_ar, sections jsonb, citations jsonb, confidence, status (`draft | pending_approval | published | rejected`), run_id FK
**approvals**: id, subject_type (`insight | briefing`), subject_id, requested_by_agent, assigned_role, status (`pending | approved | rejected | changes_requested`), decided_by FK nullable, decided_at, comment
**memory_entries**: id, kind (`decision | lesson | context`), title, content, tags text[], embedding vector, source_ref jsonb, created_by FK nullable
**chat_sessions**: id, user_id FK, title, created_at
**chat_messages**: id, session_id FK, role (`user | assistant`), content, citations jsonb, confidence, tokens_in, tokens_out, latency_ms
**audit_log**: id (bigserial), ts, actor_id nullable, actor_type (`user | agent | system`), action, subject_type, subject_id, ip, detail jsonb, prev_hash, entry_hash — `entry_hash = sha256(prev_hash + canonical_json(row))`, append-only (no UPDATE/DELETE; enforce with a DB trigger)
**api_usage**: id, ts, provider, model, purpose (`chat | agent | embedding`), tokens_in, tokens_out, cost_usd, request_id, user_id nullable

---

## 7. Module Specifications

### 7.1 LLM Gateway (`services/llm/`)
- `LLMGateway` interface: `async def complete(messages, *, system, tools=None, model=None, max_tokens, temperature, json_schema=None) -> LLMResult` where `LLMResult` carries text, parsed tool calls, usage, model, latency.
- Anthropic provider uses the official `anthropic` SDK (Messages API, tool use). OpenAI provider optional fallback. Provider + default models chosen by env (`LLM_PROVIDER`, `LLM_MODEL_PRIMARY`, `LLM_MODEL_FAST`).
- Structured output helper: request JSON conforming to a Pydantic schema; validate + one automatic repair retry on parse failure.
- Retries with exponential backoff on 429/5xx (max from env). Timeouts from env.
- Every call: log request_id, purpose, latency, tokens; write an `api_usage` row with computed cost (price table in config, overridable via env).
- Redact document content from logs when classification is OFFICIAL or higher (log ids + counts, not text).

### 7.2 RAG Subsystem (`services/rag/`)
- **Indexer:** accepts uploaded files (pdf, docx, txt, md, html). Extract text (pypdf / python-docx / bs4). Chunk ~800 tokens with 150 overlap, respecting paragraph boundaries. Embed in batches; store chunks + embeddings. Document status transitions pending→processing→indexed. Runs as an ARQ task.
- **Retriever:** `retrieve(query, *, k, classification_ceiling, language=None) -> list[RetrievedChunk]` — vector similarity via pgvector cosine, filtered by classification ≤ user clearance. Optional keyword fallback (Postgres FTS) merged by reciprocal rank fusion. Return chunk text, document title, score.
- **Grounded answer composer:** builds the chat/agent context block with numbered sources `[1]..[n]`; instructs the model to cite by number and to state when the corpus is insufficient. Confidence score = calibrated function of top-k similarity + model self-report; document the formula in code.

### 7.3 Agent Framework (`services/agents/`)
- `BaseAgent`: name, description, system prompt (loaded from versioned files in `prompts/`), allowed tools, output Pydantic schema, `run(context) -> AgentOutput`. Handles: prompt assembly, retrieval injection, tool-call loop (max iterations from env), structured output validation, usage logging, and writing a `pipeline_steps` row.
- **Tools available to agents** (implement as plain async functions with JSON schemas): `search_knowledge_base(query)`, `search_ingested_items(query, filters)`, `get_memory(query)`, `save_memory(entry)`, `get_kpi_snapshot()`.
- **The six agents:**
  1. **Signal Agent** — triage batch of `new` ingested_items: relevance (0–1), category (economic/geopolitical/regulatory/technology/social), urgency (low/med/high/critical), one-line rationale. Discards items below relevance threshold (env). Fast model.
  2. **Risk Agent** — for triaged high-relevance items: produce Risk insights (title, analysis, severity 1–5, likelihood, affected domains, 2–4 recommended actions, citations to items/chunks, confidence). Primary model.
  3. **Opportunity Agent** — mirror of Risk Agent for opportunities (impact instead of severity).
  4. **Policy Agent** — detect regulatory/policy changes; output policy insights: what changed, jurisdictions, compliance impact, required response, deadline if any.
  5. **Executive Briefing Agent** — synthesize the day's published-or-pending insights + KPI snapshot into a briefing: exec summary, top risks, top opportunities, policy watch, recommended decisions. Produce `body_en`; produce `body_ar` as a faithful Arabic rendering (second LLM pass).
  6. **Strategic Memory Agent** — after each run: extract durable decisions/lessons worth remembering into `memory_entries` (embedded for retrieval); also answers memory queries.
- Write clear, production-quality system prompts for each agent in `prompts/` (these are deliverables, not stubs): role, inputs, output JSON schema, grounding rules (cite or abstain), tone (government analyst, neutral), and Arabic output guidance.

### 7.4 Orchestrator (`services/orchestrator.py`)
- `run_pipeline(trigger, initiated_by)` executes: Signal → (Risk ∥ Opportunity ∥ Policy in parallel via asyncio.gather) → Briefing → Memory. Creates `pipeline_runs` + step rows, streams status into DB so the UI can poll `GET /api/pipeline/runs/{id}`.
- Every produced insight/briefing enters `approvals` as `pending` (nothing auto-publishes). Idempotent per day for scheduled runs. Partial-failure tolerant: a failed step marks the run `partial`, later steps that don't depend on it continue.

### 7.5 Ingestion Connectors (`services/ingestion/`)
- `BaseConnector.fetch(since) -> list[RawItem]`; normalization to `ingested_items` with dedup by `dedup_hash = sha256(source_id + external_id or url or title+date)`.
- Implement: **World Bank Indicators API** (no key; config lists indicator codes + countries), **GDELT 2.0 DOC API** (no key; query terms from source config), **generic RSS** (feedparser; N feeds via config), **ReliefWeb API** (no key). All connectors respect per-source `poll_interval_minutes`; scheduler enqueues syncs.
- Webhook receiver `POST /api/ingest/webhook/{source_id}` secured by per-source HMAC secret — the hook for future licensed feeds (Bloomberg/Reuters) without new code paths.

### 7.6 Security (`security/`)
- JWT access (15 min) + refresh (14 d, rotating, hashed at rest). `require_role(*roles)` and `require_clearance(level)` dependencies. Role→clearance mapping in config (admin/executive → OFFICIAL_SENSITIVE, analyst → OFFICIAL, viewer → INTERNAL).
- Classification enforcement at the data layer for: document retrieval, insights listing, briefings, chat grounding.
- Rate limiting (Redis sliding window): login 5/min/IP; chat 20/min/user (env-tunable).
- Audit every state-changing action and every approval decision via `audit_service` (hash chain per Section 6). `GET /api/audit` (admin) supports verification endpoint that re-walks the chain.
- SSO: create `security/oidc.py` stub with a documented interface + env vars, marked Phase 4.

### 7.7 API Layer — Endpoint Contract
All under `/api`, JSON, JWT bearer auth unless noted. Implement exactly; add response models for each.

| # | Method + Path | Purpose | Roles |
|---|---|---|---|
| 1 | POST /auth/login | email+password → access+refresh | public |
| 2 | POST /auth/refresh | rotate refresh → new pair | public |
| 3 | GET /auth/me | current user profile | any |
| 4 | POST /agent/chat | grounded chat: `{session_id?, message, language?}` → `{answer, citations[], confidence, session_id}` | any |
| 5 | GET /agent/chat/sessions & /agent/chat/sessions/{id} | history | any |
| 6 | POST /knowledge/documents | multipart upload → indexing task | analyst+ |
| 7 | GET /knowledge/documents | list + status | any |
| 8 | POST /knowledge/search | semantic search (debug/UI) | analyst+ |
| 9 | GET /sources | list sources + health | any |
| 10 | POST /sources (+PATCH /sources/{id}) | create/update source | admin |
| 11 | POST /sources/{id}/sync | manual sync now | analyst+ |
| 12 | GET /items | ingested items, filters: status, category, urgency, source, q, date range, pagination | any |
| 13 | POST /pipeline/run | trigger full run → `{run_id}` | analyst+ |
| 14 | GET /pipeline/runs & /pipeline/runs/{id} | run list / live status with steps | any |
| 15 | GET /insights | filter by kind/status/severity/domain; published only for viewer | any |
| 16 | GET /insights/{id} | detail incl. citations | any |
| 17 | GET /briefings & /briefings/{id} | list/detail (EN+AR bodies) | any |
| 18 | POST /briefings/generate | on-demand briefing (runs Briefing Agent only) | executive+ |
| 19 | GET /approvals?status=pending | approval queue | executive+ |
| 20 | POST /approvals/{id}/decision | `{decision: approved|rejected|changes_requested, comment}` → publishes/rejects subject | executive+ |
| 21 | GET /dashboard/summary | counts, latest run, top insights, source health, usage cost — single call for the UI's command centre | any |
| 22 | GET /memory & POST /memory/search | institutional memory | analyst+ |
| 23 | GET /audit & GET /audit/verify | audit trail + chain verification | admin |
| 24 | POST /ingest/webhook/{source_id} | HMAC-secured push ingestion | HMAC |
| 25 | GET /healthz, GET /metrics | liveness + Prometheus | public |

CORS: allow origins from `CORS_ORIGINS` env (comma-separated; include `null` for local file:// testing of the HTML prototype, with a code comment warning to remove in production).

### 7.8 Notifications
- `notification_service`: on approval-pending and on published briefing, send email via SMTP env config (aiosmtplib) and write an in-app notification row (add `notifications` table: id, user_id/role, kind, subject_ref, read_at). `GET /api/notifications` + mark-read. If SMTP unset, log-only mode (do not crash).

---

## 8. Seed Data (`scripts/seed.py`)
- Roles + one admin user from `ADMIN_EMAIL` / `ADMIN_INITIAL_PASSWORD` env.
- Default sources: World Bank (GDP, inflation, unemployment for a country list from env `WATCH_COUNTRIES`), GDELT query from env `WATCH_QUERY_TERMS`, 3 example RSS feeds, ReliefWeb.
- 3 small public-domain sample documents (generate brief markdown files about fictional ministry strategy) indexed at seed time so chat works immediately.

---

## 9. Build Order (implement in this exact sequence)

**Phase 0 — Skeleton (do first):** repo structure, config/Settings mirroring `.env.example`, logging, exceptions, docker-compose (postgres+pgvector, redis), Alembic migration 0001 (full schema), health endpoint, CI-style `make lint test` green.

**Phase 1 — Grounded chat:** auth (login/refresh/me, RBAC deps), LLM gateway (Anthropic provider + usage tracking), RAG indexer + retriever, document upload endpoints, `POST /api/agent/chat` with citations + confidence + sessions, seed script, tests.

**Phase 2 — Real data + first agent:** connectors (World Bank, GDELT, RSS, ReliefWeb), scheduler + sync tasks, items API, Signal Agent + Risk Agent, insights API, minimal orchestrator (Signal→Risk), dashboard summary v1.

**Phase 3 — Full agent cycle:** Opportunity, Policy, Briefing (EN+AR), Memory agents; full orchestrator with parallel fan-out; approvals workflow + publication rules; notifications; pipeline run APIs with live step status; memory APIs.

**Phase 4 — Hardening:** hash-chained audit on all mutations + verify endpoint; rate limiting; classification enforcement sweep; webhook ingestion with HMAC; metrics; RUNBOOK; OIDC stub; load-test script (`scripts/loadtest.py`, simple asyncio burst) and README production notes.

After each phase: run full test suite + `make lint`, update `docs/API.md`, and commit with message `feat(phase-N): <summary>`.

---

## 10. Acceptance Criteria (Definition of Done per phase)

**Phase 1 done when:** `docker compose up` + seed → login as admin → upload a PDF → within a minute `GET /knowledge/documents` shows `indexed` → `POST /agent/chat` about the PDF returns an answer with ≥1 citation pointing at that document and a confidence in [0,1]; asking something outside the corpus yields an explicit "not in my sources" style answer; all tests green.

**Phase 2 done when:** `POST /sources/{worldbank}/sync` ingests real indicator datapoints (visible in `GET /items`); `POST /pipeline/run` produces ≥1 Risk insight grounded in real items with citations; `GET /pipeline/runs/{id}` shows per-step token usage and cost.

**Phase 3 done when:** a full run produces risk/opportunity/policy insights + a bilingual briefing, all landing in the approvals queue; approving publishes them (visible to viewer role), rejecting hides them; memory entries are created and retrievable; notification rows created.

**Phase 4 done when:** `GET /audit/verify` returns `valid: true` over ≥100 entries; tampering with a row in the DB makes it return the broken index; rate limits return 429 with Retry-After; a viewer cannot read OFFICIAL_SENSITIVE items (integration test proves it); metrics endpoint exposes request + LLM cost counters.

---

## 11. Future Integration Note (do not do yet)
A later instruction will wire the existing v11 HTML file to this backend by replacing its mock layer with `fetch()` calls to these endpoints. Therefore: keep response shapes flat and UI-friendly, include `id`, ISO timestamps, and display-ready fields; never rename endpoints after Phase 1 without updating `docs/API.md` with a changelog entry.

## 12. What NOT To Do
- Do not build a new frontend or modify the HTML file.
- Do not call any LLM in tests (use the fake gateway fixture).
- Do not auto-publish agent outputs; everything goes through approvals.
- Do not add external SaaS dependencies (vector DBs, auth providers) beyond the stack table.
- Do not skip Arabic in briefings.
- Do not log document text or chat content at OFFICIAL+ classification.
- Do not leave any `.env` values hardcoded as defaults for secrets — fail fast at startup if required secrets are missing.

Begin with Phase 0 now. Announce a short plan, then implement.
