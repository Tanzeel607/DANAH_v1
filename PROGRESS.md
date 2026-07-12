# DANAH — Autonomous Build Progress Journal

**Run started:** 2026-07-13
**Governing docs:** `DANAH_FULL_BUILD_EXECUTION_PROMPT.md` (how) · `DANAH_CLAUDE_CODE_MASTER_PROMPT.md` (what) · `DANAH_DEVELOPER_ARCHITECTURE.md` (why)

**Legend:** `[ ]` not started · `[~]` in progress · `[x]` done

---

## Environment & Credential Status

| Item | Status | Note |
|---|---|---|
| Python 3.12 | ✅ 3.12.13 | provisioned via `uv python install 3.12` (system default was 3.14, which lacks wheels for parts of the locked stack) |
| Docker + compose | ✅ 29.4.0 / v5.1.1 | daemon reachable |
| Git | ✅ 2.53.0 | repo initialised on `main` |
| GNU Make | ✅ 4.4.1 | installed via `winget install ezwinports.make` |
| `ANTHROPIC_API_KEY` | ❌ ABSENT | |
| `OPENAI_API_KEY` | ❌ ABSENT | |
| `VOYAGE_API_KEY` | ❌ ABSENT | |

### 🔑 CREDENTIAL MODE: `PENDING-CREDENTIALS`

Per Execution Prompt Rule 8: the full system is built with **real** production code paths — no stubs, no fake keys.
All tests pass against the `FakeLLMGateway` / `FakeEmbedder` fixtures. Live acceptance checks that require a
real provider key are marked **`PENDING-CREDENTIALS`** below and are **never** marked passed. They are executable
by the user via `scripts/smoke_test.py` (`make smoke`) after adding keys — see `FIRST_RUN.md`.

---

## Phase 0 — Skeleton

| # | Step | Status | When | Note |
|---|---|---|---|---|
| 1 | Verify prerequisites; `git init`; write `.gitignore` | [x] | 2026-07-13 | py3.12 via uv, make via winget, docker OK; `.gitignore` excludes `.env` |
| 2 | Create `.env` from `.env.example`; record credential availability | [x] | 2026-07-13 | random JWT secret + admin password + HMAC secret generated; keys ABSENT → PENDING-CREDENTIALS |
| 3 | Full repo structure [§5]; `pyproject.toml` pinned deps; `Makefile` | [x] | 2026-07-13 | + `make.ps1` for Windows (DECISIONS #9) |
| 4 | `docker-compose.yml`: api, worker, scheduler, postgres+pgvector, redis | [x] | 2026-07-13 | all 5 services healthy; host ports configurable (DECISIONS #16) |
| 5 | `app/config.py` Settings mirroring every `.env.example` var + fail-fast | [x] | 2026-07-13 | bidirectional contract enforced by `test_config_contract.py` (13 tests) |
| 6 | Logging (structlog JSON + request-id) + exception hierarchy + handlers | [x] | 2026-07-13 | request id in ContextVar; `redact_text()` withholds text at OFFICIAL+ |
| 7 | Alembic init + migration 0001 (full schema, pgvector, HNSW, audit trigger) | [x] | 2026-07-13 | 17 tables, 20 enums, 2 HNSW + 2 GIN indexes; **audit trigger verified: INSERT ok, UPDATE/DELETE/TRUNCATE blocked** |
| 8 | `main.py` app factory, CORS, `/api/healthz`, `/metrics`, routers mounted | [x] | 2026-07-13 | metrics on `prometheus_client` directly (DECISIONS #19) |
| 9 | Test harness: `conftest.py`, test DB, FakeLLMGateway + FakeEmbedder | [x] | 2026-07-13 | real Postgres+pgvector test DB; semantically-meaningful fake embedder |
| 10 | **Phase 0 gate:** compose up, healthz 200, lint/mypy/pytest green; commit + tag `phase-0-complete` | [x] | 2026-07-13 | ✅ ruff clean · mypy --strict clean (42 files) · 22 tests pass · 5/5 services healthy |

## Phase 1 — Grounded chat

| # | Step | Status | When | Note |
|---|---|---|---|---|
| 11 | Security core: argon2, JWT access/refresh rotation, `require_role`/`require_clearance` | [ ] | | |
| 12 | Auth API: login, refresh, me + integration tests | [ ] | | |
| 13 | LLM gateway + Anthropic provider (tool use, structured output + repair retry, backoff) | [ ] | | |
| 14 | OpenAI provider + fallback switch; `usage_tracker` → `api_usage` with cost table | [ ] | | |
| 15 | Embeddings service (voyage/openai by env, batching) | [ ] | | |
| 16 | Indexer: extract → paragraph-aware chunk → embed → store; ARQ `embed_document` | [ ] | | |
| 17 | Retriever: pgvector cosine + Postgres FTS, RRF, classification filter in SQL | [ ] | | |
| 18 | Grounded answer composer: numbered sources, cite-or-abstain, confidence formula | [ ] | | |
| 19 | Knowledge API: upload, list, semantic search | [ ] | | |
| 20 | Chat API: sessions + `POST /api/agent/chat` (answer, citations, confidence) | [ ] | | |
| 21 | `scripts/seed.py`: admin, default sources, 3 sample docs indexed | [ ] | | |
| 22 | **Phase 1 gate:** §10 Phase-1 criteria; green; `docs/API.md`; commit + tag `phase-1-complete` | [ ] | | |

## Phase 2 — Real data + first agents

| # | Step | Status | When | Note |
|---|---|---|---|---|
| 23 | `BaseConnector` + normalization + `dedup_hash` | [ ] | | |
| 24 | World Bank, GDELT, RSS, ReliefWeb connectors + recorded-response unit tests | [ ] | | |
| 25 | ARQ scheduler: per-source polling, `sync_source` task, source health | [ ] | | |
| 26 | Sources + items APIs incl. manual sync | [ ] | | |
| 27 | `BaseAgent` framework + agent tools | [ ] | | |
| 28 | Signal Agent + versioned prompt; relevance-threshold archiving | [ ] | | |
| 29 | Risk Agent + prompt; insights persistence with citations + confidence | [ ] | | |
| 30 | Minimal orchestrator (Signal→Risk) + run/step records; pipeline APIs | [ ] | | |
| 31 | Insights API; `GET /api/dashboard/summary` v1 | [ ] | | |
| 32 | **Phase 2 gate:** §10 Phase-2 criteria; green; commit + tag `phase-2-complete` | [ ] | | |

## Phase 3 — Full agent cycle

| # | Step | Status | When | Note |
|---|---|---|---|---|
| 33 | Opportunity Agent, Policy Agent (+prompts) | [ ] | | |
| 34 | Briefing Agent: EN synthesis + faithful AR pass; briefings persistence + APIs | [ ] | | |
| 35 | Memory Agent + memory service (embedded entries) + memory APIs | [ ] | | |
| 36 | Full orchestrator: Signal → parallel(Risk, Opp, Policy) → Briefing → Memory; partial-failure; cron; token budget | [ ] | | |
| 37 | Approvals workflow: auto-pending, decision endpoint publishes/rejects, viewer sees published only | [ ] | | |
| 38 | Notifications: table + service (SMTP or log-only) + API | [ ] | | |
| 39 | **Phase 3 gate:** §10 Phase-3 criteria; green; commit + tag `phase-3-complete` | [ ] | | |

## Phase 4 — Hardening

| # | Step | Status | When | Note |
|---|---|---|---|---|
| 40 | Audit service on all mutations + hash chain + `/audit/verify` + tamper test | [ ] | | |
| 41 | Rate limiting (login, chat) with 429 + Retry-After + tests | [ ] | | |
| 42 | Classification enforcement sweep + integration tests (viewer blocked everywhere) | [ ] | | |
| 43 | Webhook ingestion with per-source HMAC + tests | [ ] | | |
| 44 | Prometheus metrics: request + LLM tokens/cost; `DAILY_COST_ALERT_USD` wiring | [ ] | | |
| 45 | OIDC stub module + env plumbing (disabled by default) | [ ] | | |
| 46 | `scripts/loadtest.py` + results in RUNBOOK | [ ] | | |
| 47 | Docs finalization: `README.md`, `docs/RUNBOOK.md`, `docs/API.md` | [ ] | | |
| 48 | **Phase 4 gate:** §10 Phase-4 criteria incl. tamper detection; green; commit + tag `phase-4-complete` | [ ] | | |

## Completion

| # | Step | Status | When | Note |
|---|---|---|---|---|
| 49 | Write `BUILD_REPORT.md` (every §10 criterion, endpoint inventory, coverage, limitations) | [ ] | | |
| 50 | Print final summary pointing to `BUILD_REPORT.md` + `FIRST_RUN.md` | [ ] | | |

---

## Acceptance Criteria Tracker (Master Prompt §10)

| Phase | Criterion | Status | Proof |
|---|---|---|---|
| 1 | compose up + seed → login → upload PDF → `indexed` within a minute | [ ] | |
| 1 | chat about the PDF → answer w/ ≥1 citation to that doc + confidence ∈ [0,1] | [ ] | |
| 1 | out-of-corpus question → explicit "not in my sources" abstention | [ ] | |
| 2 | `POST /sources/{worldbank}/sync` ingests real datapoints, visible in `GET /items` | [ ] | |
| 2 | `POST /pipeline/run` → ≥1 Risk insight grounded in real items with citations | [ ] | |
| 2 | `GET /pipeline/runs/{id}` shows per-step token usage and cost | [ ] | |
| 3 | full run → risk/opportunity/policy insights + bilingual briefing, all in approvals queue | [ ] | |
| 3 | approving publishes (visible to viewer); rejecting hides | [ ] | |
| 3 | memory entries created and retrievable; notification rows created | [ ] | |
| 4 | `GET /audit/verify` → `valid: true` over ≥100 entries | [ ] | |
| 4 | tampering with a DB row → verify returns the broken index | [ ] | |
| 4 | rate limits return 429 with `Retry-After` | [ ] | |
| 4 | viewer cannot read OFFICIAL_SENSITIVE (integration test proves it) | [ ] | |
| 4 | `/metrics` exposes request + LLM cost counters | [ ] | |
