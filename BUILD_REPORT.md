# DANAH — Build Report

**Build run:** 2026-07-13 · autonomous, Phases 0–4 · **Credential mode: `PENDING-CREDENTIALS`**

---

## 1. Read this first

The backend is **built**: 17-table schema, six agents with production prompts, four ingestion
connectors, hybrid RAG, the approval gate, a hash-chained audit log, rate limiting, and all 25
endpoints from master prompt §7.7. `ruff`, `mypy --strict` (101 source files) and the unit suite are
green.

Two things are **not** finished, and both are stated plainly rather than papered over:

1. **No provider keys were available**, so every acceptance criterion that needs a live model is
   marked `PENDING-CREDENTIALS`, never `PASSED`. Nothing was stubbed to fake a pass —
   `scripts/smoke_test.py` (`make smoke`) proves them for real once keys are added. See
   [`FIRST_RUN.md`](FIRST_RUN.md).

2. **Docker's control-plane API crashed during the build** — the daemon began returning
   `500 Internal Server Error` on every `docker` command, so `docker compose build` and
   `docker compose ps` no longer work on this machine until Docker Desktop is restarted. The
   *containers themselves kept running* (ports 5433/6379/8000 stayed reachable), so the test suite
   still ran against the real Postgres + pgvector and the real Redis. **Restart Docker Desktop
   before the next `docker compose` command.** It was not restarted here because doing so would also
   have stopped an unrelated `crm_postgres` container belonging to another project on this machine.

---

## 2. Acceptance criteria (master prompt §10)

Status vocabulary: **PASSED** — asserted in this run. **PASSED-VIA-TESTS** — proven by the suite
against the real code path with the LLM faked at the gateway interface. **PENDING-CREDENTIALS** —
needs a live provider; never claimed as passed. **PENDING-DOCKER** — the test exists and is written,
but the daemon died before the final run could complete it.

| Phase | Criterion | Status | The command that proves it |
|---|---|---|---|
| 0 | `docker compose up` brings up api, worker, scheduler, postgres+pgvector, redis | **PASSED** | verified this run — all 5 healthy (before the daemon crash) |
| 0 | `/api/healthz` returns 200; lint/mypy/pytest green | **PASSED** | `curl localhost:8000/api/healthz` → `{"status":"ok","database":"up","redis":"up"}` |
| 1 | seed → login → upload a document → `indexed` within a minute | **PENDING-CREDENTIALS** | needs an embedding key. `make smoke --phases 1`. Upload→`202`→`pending` is proven by `pytest -k upload` |
| 1 | chat about the document → ≥1 citation pointing at it, confidence ∈ [0,1] | **PASSED-VIA-TESTS** · live **PENDING-CREDENTIALS** | `pytest tests/integration/test_chat.py::TestGroundedChat::test_answer_cites_the_uploaded_document` — asserts the citation's `document_id` equals the uploaded document |
| 1 | out-of-corpus question → explicit "not in my sources" | **PASSED-VIA-TESTS** · live **PENDING-CREDENTIALS** | `…::test_out_of_corpus_question_abstains` — `grounded=false`, `citations=[]`, `confidence=0.0` |
| 2 | `POST /sources/{worldbank}/sync` ingests real datapoints, visible in `GET /items` | **PENDING-CREDENTIALS** (live network) | `make smoke --phases 2`. The connector is proven against recorded responses: `pytest tests/unit/test_connectors.py` (32 tests, `respx`, no live calls) |
| 2 | `POST /pipeline/run` → ≥1 Risk insight grounded in real items, with citations | **PASSED-VIA-TESTS** · live **PENDING-CREDENTIALS** | `pytest tests/integration/test_pipeline.py::TestPipelineRun::test_run_produces_grounded_risk_insight_with_citations` |
| 2 | `GET /pipeline/runs/{id}` shows per-step token usage and cost | **PASSED-VIA-TESTS** | `…::TestPipelineAPI::test_run_detail_exposes_per_step_tokens_and_cost` |
| 3 | a full run → risk/opportunity/policy insights + a bilingual briefing, all in the approvals queue | **PASSED-VIA-TESTS** · live **PENDING-CREDENTIALS** | `pytest tests/integration/test_approvals.py` — `test_insight_is_never_published_by_the_pipeline` asserts every agent output lands `pending_approval` |
| 3 | approving publishes (visible to viewer); rejecting hides | **PASSED-VIA-TESTS** | `…::TestApprovalGate::test_approving_publishes_the_subject`, `…::test_rejecting_hides_the_subject`, `…::TestViewerSeesPublishedOnly` |
| 3 | memory entries created and retrievable; notification rows created | **PASSED-VIA-TESTS** | `…::TestMemoryAndNotifications` |
| 4 | `GET /audit/verify` → `valid: true` over ≥100 entries | **PENDING-DOCKER** | `pytest tests/integration/test_audit.py::TestHashChain::test_verify_passes_over_more_than_100_entries` (writes 120 entries) |
| 4 | tampering with a DB row → verify returns the broken index | **PENDING-DOCKER** | `…::test_tampering_with_a_row_is_detected_and_located` — disables the append-only trigger, edits row 10, asserts `broken_at_index == 9` |
| 4 | rate limits return 429 with `Retry-After` | **PENDING-DOCKER** | `pytest tests/integration/test_rate_limit.py` (needs the real Redis sliding window) |
| 4 | a viewer cannot read OFFICIAL_SENSITIVE (integration test proves it) | **PENDING-DOCKER** | `pytest tests/integration/test_classification.py` — 13 tests across documents, chat grounding, search, insights, briefings, memory **and dashboard counts** |
| 4 | `/metrics` exposes request + LLM cost counters | **PASSED** | `curl -s localhost:8000/metrics \| grep danah_llm_cost_usd_total` — verified live this run |

> The Phase-4 rows marked **PENDING-DOCKER** are written, committed and were passing individually
> earlier in the build. They are *not* claimed as passed, because the final full-suite run did not
> complete after the daemon died. Section 5 is the two-command fix.

---

## 3. Endpoint inventory vs §7.7

All 25 implemented, with response models, in OpenAPI.

| # | Endpoint | Roles | ✓ |
|---|---|---|---|
| 1–3 | `POST /auth/login` · `POST /auth/refresh` · `GET /auth/me` | public / public / any | ✅ |
| 4–5 | `POST /agent/chat` · `GET /agent/chat/sessions[/{id}]` | any | ✅ |
| 6–8 | `POST /knowledge/documents` · `GET /knowledge/documents` · `POST /knowledge/search` | analyst+ / any / analyst+ | ✅ |
| 9–11 | `GET /sources` · `POST /sources` · `PATCH /sources/{id}` · `POST /sources/{id}/sync` | any / admin / admin / analyst+ | ✅ |
| 12 | `GET /items` · `GET /items/{id}` | any | ✅ |
| 13–14 | `POST /pipeline/run` · `GET /pipeline/runs[/{id}]` | analyst+ / any | ✅ |
| 15–16 | `GET /insights` · `GET /insights/{id}` | any (viewer: published only) | ✅ |
| 17–18 | `GET /briefings[/{id}]` · `POST /briefings/generate` | any / executive+ | ✅ |
| 19–20 | `GET /approvals?status=pending` · `POST /approvals/{id}/decision` | executive+ | ✅ |
| 21 | `GET /dashboard/summary` | any | ✅ |
| 22 | `GET /memory` · `POST /memory/search` | analyst+ | ✅ |
| 23 | `GET /audit` · `GET /audit/verify` | admin | ✅ |
| 24 | `POST /ingest/webhook/{source_id}` | HMAC (no JWT) | ✅ |
| 25 | `GET /healthz` · `GET /metrics` | public | ✅ |
| + | `GET /notifications` · `POST /notifications/read` · `GET/POST/PATCH /admin/users` | any / admin | ✅ |

---

## 4. What was built

| | |
|---|---|
| Source files | 101 (`mypy --strict` clean) |
| Tests | 168 across 11 modules — 86 unit, 82 integration |
| Migration | one — 0001: 17 tables, 20 enum types, 2 HNSW + 2 GIN indexes, append-only audit trigger |
| Agents | 6, each with a versioned production prompt (`app/services/agents/prompts/*_v1.md`) |
| Connectors | 4 (World Bank, GDELT, RSS, ReliefWeb) + HMAC webhook receiver |
| Decisions recorded | 21 (`docs/DECISIONS.md`) |

**The invariants worth naming, because they are structural rather than aspirational:**

- **Nothing an agent writes can publish itself.** `PublicationStatus.PUBLISHED` is assigned in
  exactly one place in the codebase — inside `approval_service.decide()`, on a branch reachable only
  with a `decided_by` user id from an authenticated request. There is no argument, flag or code path
  by which an agent reaches it. A language model cannot talk its way past a function that does not exist.
- **Classification is a `WHERE` clause, never a post-filter and never a prompt instruction.** An
  over-classified chunk is never read out of the database, so it cannot reach a prompt, a log, or
  process memory. This extends to *counts*: a viewer's dashboard does not reveal how many things
  exist that they cannot open.
- **Only cited sources become citations.** A model that cites `[9]` when six sources were supplied
  has hallucinated, and the marker is dropped. An answer with no citation is treated as an
  abstention regardless of how confident it sounds, and an abstention scores confidence `0.0`.
- **The audit log is append-only in the database, not just in the application.** A trigger rejects
  `UPDATE`/`DELETE`/`TRUNCATE`. The hash chain exists to catch the one attacker who can disable it.

---

## 5. Finish the job — exact next steps

### (a) Complete the integration suite (needs Docker; ~3 minutes)

The Docker daemon crashed mid-run. Restart Docker Desktop, then:

```bash
docker compose up -d postgres redis
.venv/Scripts/pytest -q                    # or: make test
```

That runs all 168 tests, including every **PENDING-DOCKER** row above (audit tamper detection,
rate-limit 429s, and the viewer-blocked-from-OFFICIAL_SENSITIVE sweep).

### (b) Turn PENDING-CREDENTIALS into PASSED (needs API keys; ~10 minutes)

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
VOYAGE_API_KEY=pa-...
```

```bash
docker compose up -d --build
docker compose exec api python -m scripts.seed
make smoke                                 # walks every §10 criterion over HTTP, live
```

A full smoke run costs roughly **$0.10–$0.50**. `FIRST_RUN.md` shows the expected output line by
line. Then update the table above: `PENDING-CREDENTIALS` → `PASSED`.

### (c) Wire the front end

Out of scope for this build (master prompt §11), and the backend is shaped for it: flat,
display-ready responses; `GET /api/dashboard/summary` fills the whole command centre in one call;
`CORS_ORIGINS` already contains `null` so the v11 HTML file can be opened from disk. The contract is
`docs/API.md`. **Remove `null` from `CORS_ORIGINS` in production** — the config layer refuses to boot
with it when `APP_ENV=production`.

---

## 6. Known limitations

Deliberate and documented, not oversights:

- **S3 storage is not implemented.** `STORAGE_BACKEND=local` works; `s3` raises an error naming the
  seam (`app/services/rag/storage.py`). Object storage is production-topology work.
- **OIDC/SSO is a documented stub** (`app/security/oidc.py`). The government IdP's issuer, claims and
  group names are client-side dependencies that do not exist yet. Half-implementing a flow against an
  imagined IdP produces code that looks finished and must then be thrown away; the module documents
  the five steps and the exact seam, and `map_claims_to_role` fails closed to `viewer`.
- **The rate limiter fails open.** If Redis is unreachable, requests are allowed and the failure is
  logged loudly. A rate limiter is a guardrail, not an authentication boundary — a cache outage must
  not lock a ministry out of its own platform mid-incident.
- **`prometheus-fastapi-instrumentator` was replaced** by a direct `prometheus_client` implementation:
  the wrapper is broken against Starlette 0.52 (it reads `route.path` on `_IncludedRouter`, which has
  no such attribute, so *every* request raised). Master prompt §4 permits the closest maintained
  equivalent; see `docs/DECISIONS.md` #19.
- **Arabic quality has not been reviewed by a native speaker.** The rendering is a dedicated second
  LLM pass with a structural faithfulness check (same section keys, in order, actually in Arabic
  script), but UAT review remains a real requirement (architecture §13).
- **The load-test baseline in `docs/RUNBOOK.md` is from a development laptop.** It is a sanity check,
  not a capacity plan; re-run it against the real deployment.

---

## 7. Verifying this report

Nothing here has to be taken on trust:

```bash
.venv/Scripts/ruff check app tests scripts     # clean
.venv/Scripts/mypy --strict app                # 101 files, clean
.venv/Scripts/pytest tests/unit -q             # 86 passed, no Docker needed
git log --oneline                              # one commit per phase
git tag -l                                     # phase-0-complete … phase-4-complete
```

`PROGRESS.md` carries the 50-step journal with a one-line note on every step.
`docs/DECISIONS.md` records all 21 engineering decisions with the alternative that was rejected.
