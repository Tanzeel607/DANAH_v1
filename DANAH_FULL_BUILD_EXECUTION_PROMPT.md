# DANAH — Full Autonomous Build: Execution Prompt (All Phases, One Run)

> **How to use:** Place this file in the project folder together with `DANAH_CLAUDE_CODE_MASTER_PROMPT.md`, `DANAH_DEVELOPER_ARCHITECTURE.md`, `.env.example`, and the v11 HTML prototype. Then tell Claude Code: *"Read DANAH_FULL_BUILD_EXECUTION_PROMPT.md and execute it."* This file governs HOW the build runs; the master prompt governs WHAT is built. If they ever conflict, the master prompt wins on scope and the acceptance criteria in its Section 10 are non-negotiable.

---

## Mission

Execute the complete DANAH backend build — Phases 0, 1, 2, 3, and 4 of the master prompt — in one continuous, autonomous session, step by step, without stopping between phases for approval. Do not summarize the plan and wait; plan briefly, then build.

---

## Autonomous Run Rules

1. **Read first.** Fully read `DANAH_CLAUDE_CODE_MASTER_PROMPT.md` and `DANAH_DEVELOPER_ARCHITECTURE.md` before writing any code. The HTML file is read-only reference for response shapes; never modify it.
2. **Strict phase order.** Complete each phase's steps and verification before starting the next. Within a phase, follow the step list below in order.
3. **Never stop to ask.** When a decision is needed, choose the most standard, most secure option consistent with the master prompt, record it in `docs/DECISIONS.md` (one line: decision, reason, alternative rejected), and continue. The ONLY permitted stop is a missing system prerequisite you cannot install (Docker engine, Python 3.12+); if that happens, print exactly what the user must install and how, then wait.
4. **Progress journal.** Maintain `PROGRESS.md` at the repo root: the full step checklist below with status (`[ ] / [~] / [x]`), timestamps, and one-line notes. Update it after EVERY step. If the session is resumed or context is compacted, re-read `PROGRESS.md` and `docs/DECISIONS.md` first and continue from the first unchecked step — never restart from scratch.
5. **Git discipline.** `git init` at step 1 if needed. Conventional commit after every completed step (`feat(phase1): rag retriever with hybrid search`). Tag each phase completion: `phase-0-complete` … `phase-4-complete`.
6. **Green-before-forward.** At every phase boundary run: `make lint` (ruff), `mypy --strict` on `app/`, and the full `pytest` suite. All green or you do not proceed. Fixing means fixing root causes — never delete or skip tests to pass, never loosen acceptance criteria, never replace a real implementation with a stub to get green.
7. **Failure policy.** If a step fails, diagnose and attempt up to 3 genuinely different fixes. If a specified library is unavailable in this environment, substitute the closest maintained equivalent per master prompt Section 4 and log it in `docs/DECISIONS.md`. Then continue.
8. **Credentials-aware verification.** Read `.env` (create it from `.env.example` at step 2 if absent, generating a random `JWT_SECRET_KEY` and keeping local dev DB/Redis defaults).
   - If `ANTHROPIC_API_KEY` (or the configured provider's key) and the embedding key ARE present: run the live smoke tests at each phase boundary (defined below).
   - If keys are ABSENT: build everything anyway. All tests must pass using the fake LLM gateway fixture. Do NOT invent fake keys, do NOT stub production code paths. Instead create `scripts/smoke_test.py` (an end-to-end live check runner covering every acceptance criterion in master prompt Section 10) and `FIRST_RUN.md` (exact commands the user runs after adding keys: migrate → seed → smoke test → expected outputs). Mark those checks `PENDING-CREDENTIALS` in `PROGRESS.md` — never mark them passed.
9. **No scope drift.** No frontend work, no extra features, no endpoint renames, no auto-publishing agent outputs, no external SaaS beyond the locked stack.
10. **Security constants.** Never print secret values to the terminal or logs; never commit `.env`; verify `.gitignore` covers it at step 1.

---

## Step-by-Step Execution Plan

Execute in this exact order. Master prompt section references in brackets.

### Phase 0 — Skeleton
1. Verify prerequisites (python3.12+, docker compose, git); `git init`; write `.gitignore`.
2. Create `.env` from `.env.example` per Rule 8; detect and record credential availability in `PROGRESS.md`.
3. Create full repo structure [§5]; `pyproject.toml` with pinned dependencies; `Makefile` (dev, test, lint, typecheck, migrate, seed, smoke).
4. `docker-compose.yml`: api, worker, scheduler, postgres:16+pgvector, redis:7; healthchecks; volumes.
5. `app/config.py` Settings mirroring every `.env.example` variable, with fail-fast validation for [REQUIRED] secrets in production env.
6. Logging (structlog JSON + request-id middleware), exception hierarchy + global handlers [§3.6–3.7].
7. Alembic init + migration 0001: complete schema incl. pgvector extension, HNSW index, audit append-only trigger [§6].
8. `main.py` app factory, CORS from env, `/api/healthz`, `/metrics`; empty routers mounted.
9. Test harness: `tests/conftest.py` with test DB (separate schema or db), app fixture, FakeLLMGateway + FakeEmbedder fixtures.
10. **Phase 0 gate:** compose up succeeds; healthz 200; lint/mypy/pytest green; commit + tag.

### Phase 1 — Grounded chat
11. Security core: argon2 hashing, JWT access/refresh with rotation, `require_role` / `require_clearance` deps [§7.6].
12. Auth API: login, refresh, me [§7.7 #1–3] + integration tests.
13. LLM gateway interface + Anthropic provider (tool use, structured output w/ repair retry, retries/backoff, timeouts) [§7.1].
14. OpenAI provider + fallback switch; `usage_tracker` writing `api_usage` with cost table.
15. Embeddings service (voyage/openai by env, batching) [§7.2].
16. Indexer: extraction (pdf/docx/txt/md/html) → paragraph-aware chunking → embed → store; ARQ task `embed_document`; document status lifecycle.
17. Retriever: pgvector cosine top-k + Postgres FTS, reciprocal rank fusion, classification filter in SQL.
18. Grounded answer composer: numbered sources, cite-or-abstain instruction, confidence formula (documented).
19. Knowledge API: upload (multipart, size/ext limits from env), list, semantic search [#6–8].
20. Chat API: sessions + `POST /api/agent/chat` returning answer, citations[], confidence [#4–5]; persistence of messages with usage.
21. `scripts/seed.py`: admin user, roles, default sources (rows only), 3 sample strategy docs indexed.
22. **Phase 1 gate:** run master prompt §10 Phase-1 criteria (live if keys, else via tests + smoke script); lint/mypy/pytest green; update `docs/API.md`; commit + tag.

### Phase 2 — Real data + first agents
23. Connector framework `BaseConnector` + normalization + dedup_hash [§7.5].
24. World Bank connector (indicators × WATCH_COUNTRIES); GDELT DOC connector (WATCH_QUERY_TERMS); RSS connector; ReliefWeb connector — each with a recorded-response unit test (no live calls in tests).
25. ARQ scheduler: per-source polling by `poll_interval_minutes`; `sync_source` task; source health fields.
26. Sources + items APIs [#9–12] incl. manual sync.
27. Agent framework `BaseAgent` (prompt loading, retrieval injection, tool loop, schema-validated output, step logging) [§7.3] + agent tools.
28. Signal Agent + versioned prompt; relevance threshold archiving.
29. Risk Agent + prompt; insights persistence with citations + confidence.
30. Minimal orchestrator (Signal→Risk) + pipeline run/step records; pipeline APIs [#13–14].
31. Insights API [#15–16]; `GET /api/dashboard/summary` v1 [#21].
32. **Phase 2 gate:** §10 Phase-2 criteria (live World Bank sync if network allows; otherwise recorded fixtures + smoke script entry); green; commit + tag.

### Phase 3 — Full agent cycle
33. Opportunity Agent, Policy Agent (+prompts).
34. Briefing Agent: EN synthesis + faithful AR second pass; briefings persistence + APIs [#17–18].
35. Memory Agent + memory service (embedded entries) + memory APIs [#22].
36. Full orchestrator: Signal → parallel(Risk, Opportunity, Policy) → Briefing → Memory; partial-failure semantics; daily cron; per-run token budget enforcement.
37. Approvals workflow: auto-create pending approvals for every insight/briefing; decision endpoint publishes/rejects; viewer sees published only [#19–20].
38. Notifications: table + service (SMTP or log-only) + `GET /api/notifications`, mark-read [§7.8].
39. **Phase 3 gate:** §10 Phase-3 criteria; green; commit + tag.

### Phase 4 — Hardening
40. Audit service on ALL state-changing endpoints + agent/system actors; hash chain verified by `GET /api/audit/verify`; tamper test [§7.6].
41. Rate limiting middleware (login, chat) with 429 + Retry-After; tests.
42. Classification enforcement sweep: integration tests proving viewer cannot access OFFICIAL_SENSITIVE via ANY read path (documents, chunks in chat grounding, insights, briefings, memory).
43. Webhook ingestion with per-source HMAC verification [#24] + tests.
44. Prometheus metrics: request latency/errors + LLM tokens/cost counters; wire `DAILY_COST_ALERT_USD` notification.
45. OIDC stub module + env plumbing (disabled by default), documented.
46. `scripts/loadtest.py` (async burst against chat + dashboard); record results in RUNBOOK.
47. Docs finalization: `README.md`, `docs/RUNBOOK.md`, `docs/API.md` complete and accurate against code.
48. **Phase 4 gate:** §10 Phase-4 criteria incl. audit tamper detection; full suite green; commit + tag `phase-4-complete`.

### Completion
49. Write `BUILD_REPORT.md`: table of every §10 acceptance criterion with status PASSED / PASSED-VIA-TESTS / PENDING-CREDENTIALS and the command that proves it; endpoint inventory vs §7.7; test counts + coverage; known limitations; exact next steps (add keys → `make smoke` → frontend integration).
50. Print a short final summary in the terminal pointing to `BUILD_REPORT.md` and `FIRST_RUN.md`.

---

## Definition of DONE for this run
- All 50 steps checked in `PROGRESS.md`; tags `phase-0-complete` … `phase-4-complete` exist.
- `ruff`, `mypy --strict`, and `pytest` fully green from a clean clone via `make lint typecheck test`.
- Every §7.7 endpoint implemented with response models and appearing in OpenAPI docs.
- `BUILD_REPORT.md` accounts for every acceptance criterion with no criterion silently skipped.
- Zero modifications to the v11 HTML file; zero secrets in git history.

Begin now: read the master prompt and architecture document, write the initial `PROGRESS.md` checklist, then start Step 1.
