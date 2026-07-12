# FIRST RUN — bringing DANAH to life

This build completed in **`PENDING-CREDENTIALS`** mode: no LLM or embedding provider key was
available at build time.

**What that means:** every production code path is real and complete. Nothing was stubbed, faked
or short-circuited to make a test pass. The full suite is green against the fake gateway — which
substitutes at the *provider interface*, so the agents, retriever, composer, orchestrator, API and
database are all exercised for real.

**What it does not mean:** the acceptance criteria that require a live model (a real cited answer,
a real Arabic briefing, real token costs) have **not** been asserted against a real provider. They
are marked `PENDING-CREDENTIALS` in [`BUILD_REPORT.md`](BUILD_REPORT.md) and in
[`PROGRESS.md`](PROGRESS.md), never as passed.

This page is how you close that gap. It takes about ten minutes.

---

## 1. Add your provider keys

Open `.env` (it already exists, with a randomly generated `JWT_SECRET_KEY`,
`ADMIN_INITIAL_PASSWORD` and `WEBHOOK_HMAC_DEFAULT_SECRET` — do not commit it) and set:

```dotenv
# The reasoning provider. Anthropic is the default.
ANTHROPIC_API_KEY=sk-ant-...

# The embedding provider for RAG. Voyage is the default.
VOYAGE_API_KEY=pa-...
```

**Using OpenAI for everything instead?** Set these four lines and leave the Anthropic/Voyage keys blank:

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
EMBEDDING_PROVIDER=openai
EMBEDDING_DIM=1536          # text-embedding-3-small's native dimension
```

> ⚠️ **`EMBEDDING_DIM` is load-bearing.** The `vector(n)` column is created by migration 0001
> from this value. If you change it after migrating, drop the volume and re-migrate
> (`docker compose down -v && docker compose up -d`), or every insert will fail on a dimension
> mismatch. The config layer refuses to start if `EMBEDDING_DIM` is impossible for the chosen model.

---

## 2. Start the stack

```bash
docker compose up -d --build
docker compose ps            # api, worker, scheduler, postgres, redis — all "healthy"
```

`api` runs `alembic upgrade head` on start, so the schema is created for you.

Confirm the keys were picked up:

```bash
curl -s localhost:8000/api/healthz | jq
# "llm_configured": true, "embeddings_configured": true   ← both must be true
```

If either is `false`, the container did not see your `.env`. Rebuild: `docker compose up -d --build`.

---

## 3. Seed

```bash
docker compose exec api python -m scripts.seed
```

This creates the admin user, the four open data sources (World Bank, GDELT, RSS, ReliefWeb), and
**indexes three sample ministry-strategy documents** so chat has a corpus from the first minute.

The admin password is the `ADMIN_INITIAL_PASSWORD` in your `.env`. **Change it after first login.**

---

## 4. Prove it works

```bash
make smoke            # or: python -m scripts.smoke_test
```

`scripts/smoke_test.py` walks **every acceptance criterion in master prompt §10** against the
running stack, over HTTP, with the real model. Expected output:

```
Phase 0 — service health
  ✔ [0] GET /api/healthz returns 200 with database and redis up

Phase 1 — grounded chat
  ✔ [1] login as admin returns an access token
  ✔ [1] upload a document (202 accepted)
  ✔ [1] document reaches status 'indexed' within a minute
  ✔ [1] chat answers with >=1 citation pointing at the uploaded document
  ✔ [1] confidence is in [0,1]
  ✔ [1] out-of-corpus question yields an explicit abstention, not an invention

Phase 2 — real data + agents
  ✔ [2] POST /api/sources/{worldbank}/sync ingests real indicator datapoints
  ✔ [2] ingested datapoints are visible in GET /api/items
  ✔ [2] pipeline run completes
  ✔ [2] GET /api/pipeline/runs/{id} shows per-step token usage and cost
  ✔ [2] pipeline produces >=1 Risk insight grounded in real items with citations

Phase 3 — full agent cycle
  ✔ [3] risk / opportunity / policy insights produced
  ✔ [3] briefing carries BOTH an English and a real Arabic body
  ✔ [3] every agent output lands in the approvals queue as pending
  ✔ [3] approving publishes the subject
  ✔ [3] memory entries are created and retrievable
  ✔ [3] notification rows are created

Phase 4 — hardening
  ✔ [4] GET /api/audit/verify returns valid: true over the whole chain
  ✔ [4] rate limit returns 429 with a Retry-After header
  ✔ [4] /metrics exposes request and LLM cost counters

All checked acceptance criteria passed.
```

Run a single phase with `python -m scripts.smoke_test --phases 1`.

**A full smoke run costs real money** — roughly **$0.10–$0.50** on Claude Sonnet, depending on how
many items the pipeline picks up. `PIPELINE_TOKEN_BUDGET` (default 400k tokens) is the hard ceiling
per run; `DAILY_COST_ALERT_USD` notifies administrators when the daily spend is exceeded.

Then update `BUILD_REPORT.md`: change the `PENDING-CREDENTIALS` rows to `PASSED`.

---

## 5. Try it by hand

```bash
# log in
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@ministry.gov","password":"<ADMIN_INITIAL_PASSWORD>"}' | jq -r .access_token)

# ask a grounded question about the seeded corpus
curl -s -X POST localhost:8000/api/agent/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"What is the non-oil GDP target and by when?"}' | jq

# ask something outside the corpus — it must abstain, not invent
curl -s -X POST localhost:8000/api/agent/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"What is the capital of Atlantis?"}' | jq '.grounded, .answer'

# pull real World Bank data, then run the agent pipeline over it
SRC=$(curl -s localhost:8000/api/sources -H "Authorization: Bearer $TOKEN" \
  | jq -r '.[] | select(.connector=="worldbank") | .id')
curl -s -X POST localhost:8000/api/sources/$SRC/sync -H "Authorization: Bearer $TOKEN" | jq
RUN=$(curl -s -X POST localhost:8000/api/pipeline/run -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{}' | jq -r .run_id)

# watch the agents work, with live token cost per step
watch -n3 "curl -s localhost:8000/api/pipeline/runs/$RUN -H 'Authorization: Bearer $TOKEN' \
  | jq '{status, total_cost_usd, steps: [.steps[] | {agent, status, tokens_in, cost_usd}]}'"

# nothing publishes itself — everything waits for a human
curl -s 'localhost:8000/api/approvals?status=pending' -H "Authorization: Bearer $TOKEN" | jq
```

Interactive docs: <http://localhost:8000/docs>

---

## 6. Wiring the v11 front end

Not part of this build (master prompt §11), but the backend is ready for it:

- `CORS_ORIGINS` already contains `null`, so the HTML file can be opened straight from disk during
  development. **Remove `null` in production** — the config layer refuses to start with it if
  `APP_ENV=production`.
- Every response is flat and display-ready: ids, ISO timestamps, and pre-computed fields
  (`health`, `impact`, `total_cost_usd`) so the UI does no arithmetic.
- `GET /api/dashboard/summary` is a single call that fills the entire command centre.
- The endpoint contract is in [`docs/API.md`](docs/API.md), and it matches master prompt §7.7
  exactly. Endpoints are not renamed after Phase 1 without a changelog entry there.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `healthz` shows `llm_configured: false` | container did not see `.env` | `docker compose up -d --build` |
| Chat returns `503 llm_not_configured` | no provider key | add `ANTHROPIC_API_KEY`, restart |
| Document stuck on `pending` | worker down, or Redis unreachable | `docker compose logs worker` |
| Document reads `failed` | check the `error` field | scanned PDF → needs OCR first |
| Every insert fails on dimension | `EMBEDDING_DIM` ≠ the model's output | fix `.env`, then `docker compose down -v && up` |
| `port 5432 already allocated` | another Postgres owns the port | set `POSTGRES_HOST_PORT=5433` in `.env` |
| Answers cite nothing, always abstain | corpus is empty or unindexed | `make seed`; confirm `status: indexed` |

Operational detail — backups, re-driving the queue, rotating keys, reindexing after a model
change — is in [`docs/RUNBOOK.md`](docs/RUNBOOK.md).
