# DANAH — External APIs & Credentials

What the client actually has to procure, and what they don't.

**Short version:** DANAH needs **two paid accounts** to function — one language-model provider and
one embedding provider. Everything else is either open (no key), self-hosted, or optional.

---

## 1. Required — the system cannot work without these

| # | Service | What it does in DANAH | Env vars | Cost model |
|---|---|---|---|---|
| 1 | **Anthropic Claude** *(primary LLM)* | Powers the grounded chat and all six agents (Signal, Risk, Opportunity, Policy, Briefing, Memory). | `ANTHROPIC_API_KEY`<br>`LLM_MODEL_PRIMARY=claude-sonnet-4-5`<br>`LLM_MODEL_FAST=claude-haiku-4-5` | pay-per-token |
| 2 | **Voyage AI** *(primary embeddings)* | Turns documents and memory into vectors so retrieval (RAG) can find them. Without it, chat has nothing to cite. | `VOYAGE_API_KEY`<br>`EMBEDDING_MODEL=voyage-3.5`<br>`EMBEDDING_DIM=1024` | pay-per-token (very cheap) |

### The all-OpenAI alternative (one account instead of two)

If the client would rather sign **one** contract, OpenAI can do both jobs. Set:

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL_PRIMARY=gpt-4o
OPENAI_MODEL_FAST=gpt-4o-mini

EMBEDDING_PROVIDER=openai
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536          # ⚠️ must be set BEFORE the first migration
```

> ⚠️ **`EMBEDDING_DIM` is load-bearing.** The `vector(n)` database column is created from it at
> migration time. Changing it later means dropping and rebuilding the vector column and re-embedding
> the entire corpus (`docs/RUNBOOK.md` §9). Decide the provider **before** go-live.

Both providers are supported by the same code — the choice is one environment variable, not a
rewrite. That was the point of the LLM gateway (`docs/DECISIONS.md` #5).

### Optional resilience

| Service | Purpose | Env |
|---|---|---|
| The *other* provider | Automatic failover if the primary is down | `LLM_FALLBACK_ENABLED=true` + both keys set |

---

## 2. No key required — already built and working

These four sources are **open data**. They are implemented, tested, and seeded by default. The
client procures **nothing**.

| Source | What DANAH pulls | Configured by |
|---|---|---|
| **World Bank Indicators API** | GDP growth, inflation, unemployment for the watched countries | `WATCH_COUNTRIES`, `WORLDBANK_INDICATORS` |
| **GDELT 2.0 DOC API** | Global news matching the ministry's watch terms | `WATCH_QUERY_TERMS` |
| **RSS / Atom feeds** | Any feed list (BBC Business, Al Jazeera by default) | `RSS_FEEDS` |
| **ReliefWeb API** | Humanitarian and disaster reports | `RELIEFWEB_COUNTRY_FILTER` |

No registration, no rate-limit contract, no cost. They are enough to run a working pilot on their
own.

---

## 3. Self-hosted — no third party at all

| Component | Notes |
|---|---|
| **PostgreSQL 16 + pgvector** | The single source of truth: documents, vectors, insights, audit chain. Runs in `docker compose`, or use a managed/sovereign Postgres. **No vector SaaS** — that was a deliberate sovereignty decision (`docs/DECISIONS.md` #2). |
| **Redis 7** | Cache, rate limits, background job queue. Runs in `docker compose`. |

---

## 4. Secrets the client generates themselves (not purchased)

These are **not** vendor credentials. Generate them; never commit them.

| Env var | What it is | How to generate |
|---|---|---|
| `JWT_SECRET_KEY` | Signs access tokens | `openssl rand -hex 48` |
| `ADMIN_INITIAL_PASSWORD` | First admin login (change after) | any strong password |
| `POSTGRES_PASSWORD` | Database password | any strong password |
| `WEBHOOK_HMAC_DEFAULT_SECRET` | Signs inbound webhook pushes | `openssl rand -hex 32` |

> The app **refuses to start in production** if any of these is missing or still holds a
> `CHANGE_ME` placeholder. That is deliberate.

---

## 5. Optional — nice to have, degrades gracefully without

| Service | What it adds | Env | If absent |
|---|---|---|---|
| **SMTP server** | Emails approvers when something needs a decision, and on published briefings | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_USE_TLS` | **Log-only mode.** In-app notifications still work; nothing crashes. Any ministry mail relay works — no vendor needed. |
| **S3-compatible object storage** | Stores original uploaded documents outside the container | `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION` | **Local disk is used** (`STORAGE_BACKEND=local`). ⚠️ The S3 path is **not implemented** in this build — it raises a clear error naming the seam. A sovereign MinIO is the intended target. |
| **Sentry** | Error tracking | `SENTRY_DSN` | Nothing. Structured JSON logs + `/metrics` already cover observability. |
| **Government OIDC / SSO** | Staff log in with their ministry identity | `OIDC_ENABLED`, `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | **Email + password login** (argon2, JWT). ⚠️ OIDC is a **documented stub** — the IdP's issuer, claim names and group→role mapping are client-side facts that do not exist yet. See §7. |

---

## 6. Future — licensed feeds (nothing to build)

The client may eventually want commercial intelligence. **No code change is needed** — the webhook
receiver is the seam, and licensed data lands in the same table, deduplicated by the same rule, and
flows into the same agent pipeline.

| Feed | Status | How it arrives |
|---|---|---|
| **Bloomberg** | not procured | `POST /api/ingest/webhook/{source_id}`, HMAC-signed |
| **Reuters** | not procured | same |
| **Financial Times** | not procured | same |
| **NewsAPI** | optional | same, or a small new connector class |

This is why the architecture insisted on a connector abstraction: procurement delays cannot block
the build, and a signed contract does not trigger a rewrite.

---

## 7. What the client must still supply (not an API — information)

These are **decisions and facts**, not credentials, and the build cannot invent them:

1. **The government IdP details** — issuer URL, client id/secret, and *which AD/LDAP group maps to
   which DANAH role* (`admin` / `executive` / `analyst` / `viewer`). Until these exist, OIDC stays a
   stub; `app/security/oidc.py` documents the exact five steps and the single function
   (`map_claims_to_role`) that the mapping drops into.
2. **The hosting decision** — self-hosted, sovereign cloud, or on-prem. This determines whether a
   sovereign LLM endpoint is needed instead of the public Anthropic/OpenAI APIs (the gateway
   supports pointing at a different endpoint).
3. **A native Arabic reviewer for UAT.** The bilingual briefing is a dedicated second LLM pass with
   a structural faithfulness check, but no one has yet reviewed its register for a ministerial reader.

---

## 8. Indicative running cost

List prices, USD per **million tokens**. Verify against current vendor pricing before quoting a
client — these move.

| Model | Input | Output | Used for |
|---|---|---|---|
| `claude-sonnet-4-5` | ~$3.00 | ~$15.00 | Risk / Opportunity / Policy / Briefing — where judgement matters |
| `claude-haiku-4-5` | ~$1.00 | ~$5.00 | Signal triage + Memory — high volume, cheap tier |
| `voyage-3.5` (embeddings) | ~$0.06 | — | Indexing documents and memory |
| `gpt-4o` / `gpt-4o-mini` | ~$2.50 / ~$0.15 | ~$10.00 / ~$0.60 | The OpenAI alternative |
| `text-embedding-3-small` | ~$0.02 | — | The OpenAI embedding alternative |

**Rough shape of the bill:** one full daily pipeline run over ~150 items lands in the region of
**$0.10 – $0.50**. Chat is a few cents per question. Embedding a document corpus is negligible
(cents per thousand pages).

**Three cost controls are already built in, not bolted on later:**

- `PIPELINE_TOKEN_BUDGET` (default 400,000 tokens) **hard-caps a single run.** A run that hits it
  stops and reports `partial` rather than billing without limit.
- **Model tiering** — the Signal Agent runs on the cheap tier and archives everything below
  `SIGNAL_RELEVANCE_THRESHOLD`, so the expensive agents only ever see items that survived triage.
- `DAILY_COST_ALERT_USD` notifies administrators when the day's spend crosses the threshold, and
  `api_usage` is a per-model, per-purpose, per-user ledger exposed on `/metrics` and the dashboard.

---

## 9. The minimum viable `.env`

Everything a working deployment needs. Two purchased keys; the rest is generated or open.

```dotenv
# --- The only two things to buy -------------------------------------------
ANTHROPIC_API_KEY=sk-ant-...
VOYAGE_API_KEY=pa-...

# --- Generate these; never commit them ------------------------------------
JWT_SECRET_KEY=<openssl rand -hex 48>
ADMIN_INITIAL_PASSWORD=<strong password>
POSTGRES_PASSWORD=<strong password>
WEBHOOK_HMAC_DEFAULT_SECRET=<openssl rand -hex 32>

# --- Infrastructure (docker compose provides both) ------------------------
DATABASE_URL=postgresql+asyncpg://danah:<pw>@postgres:5432/danah
REDIS_URL=redis://redis:6379/0

# --- Production hardening --------------------------------------------------
APP_ENV=production
APP_DEBUG=false
CORS_ORIGINS=https://danah.ministry.gov      # ⚠️ REMOVE the dev `null` origin
```

The four open data sources, the ministry watch list, model tiers and cost guardrails all have
working defaults in [`.env.example`](../.env.example) — the client only overrides what they care about.

---

## 10. One-line answer for the client

> DANAH needs **two paid API accounts**: **Anthropic Claude** (the reasoning engine) and
> **Voyage AI** (embeddings for search). Both can be replaced by a single **OpenAI** account if the
> ministry prefers one contract. Every data source in the pilot — World Bank, GDELT, news feeds,
> ReliefWeb — is **open and needs no key**. The database and cache are **self-hosted**, so no data
> leaves the ministry's boundary except the model calls themselves. SMTP, SSO and object storage are
> optional and the system runs without them. Expected model spend is on the order of **tens of
> dollars a month** for a pilot, hard-capped per run.
