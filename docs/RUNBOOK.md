# DANAH — Operations Runbook

For whoever is on call. Every procedure here is written to be followed at 3am by someone who did
not build the system.

**Golden rule:** DANAH never publishes anything without a human. If something looks wrong with an
insight or a briefing, it is sitting in the approvals queue — reject it. Nothing reaches an
executive on its own.

---

## 1. Is it up?

```bash
curl -s localhost:8000/api/healthz | jq
```

```json
{ "status": "ok", "database": "up", "redis": "up",
  "llm_configured": true, "embeddings_configured": true }
```

| Field | If it's wrong |
|---|---|
| `database: down` | §3 — Postgres |
| `redis: down` | §4 — Redis / the queue |
| `llm_configured: false` | no provider key in `.env`; chat returns `503 llm_not_configured` |
| HTTP request fails entirely | §2 — the API |

```bash
docker compose ps                      # all five services should read "healthy"
docker compose logs -f api --tail 100
```

Logs are JSON, one object per line. Every line carries `request_id`. To follow one request end to
end, across the API *and* the worker:

```bash
docker compose logs api worker | jq -c 'select(.request_id=="<id>")'
```

---

## 2. The API is down or erroring

```bash
docker compose logs api --tail 200 | jq -c 'select(.level=="error")'
docker compose restart api
```

**It won't start at all.** The most common cause is configuration: `app/config.py` fails fast and
says exactly what is wrong. Read the first error line.

```
Invalid configuration (see .env.example):
  - JWT_SECRET_KEY is required but empty
  - APP_DEBUG must be false in production
```

That is the app refusing to boot insecurely. Fix `.env`; do not work around it.

**Every request 500s.** Look for `unhandled_exception` in the logs — it carries `request_id` and
the exception type. The client got that same `request_id`, so a user report is directly traceable.

---

## 3. Postgres

```bash
docker compose exec postgres psql -U danah -d danah -c "SELECT 1"
docker compose logs postgres --tail 50
```

**Connections exhausted** (`too many clients`): `DB_POOL_SIZE` × replicas exceeds Postgres's
`max_connections`. Lower `DB_POOL_SIZE`, or raise the server's limit. The API's pool is
`DB_POOL_SIZE + DB_MAX_OVERFLOW` per process.

**Slow retrieval.** Confirm the HNSW index is being used:

```sql
EXPLAIN ANALYZE
SELECT id FROM document_chunks
ORDER BY embedding <=> '[...]'::vector LIMIT 8;
```

Look for `Index Scan using hnsw_document_chunks_embedding`. A `Seq Scan` means the index is
missing (did migration 0001 run?) or the corpus is too small for the planner to bother — the
latter is fine.

**Backups.** The database holds everything: the corpus, its vectors, insights, the audit chain.

```bash
docker compose exec postgres pg_dump -U danah -Fc danah > danah-$(date +%F).dump
# restore
docker compose exec -T postgres pg_restore -U danah -d danah --clean --if-exists < danah-2026-07-13.dump
```

> ⚠️ Back up `data/documents/` (the original uploads) too. The database stores the *path* to each
> original, not its bytes. A database-only restore leaves every document unreadable if it ever
> needs re-indexing.

---

## 4. The queue (Redis + ARQ)

The worker does the slow work: indexing documents, syncing sources, running the agent pipeline.
Symptom of a dead worker: **documents stay `pending` forever** and pipeline runs never leave
`running`.

```bash
docker compose logs worker --tail 100
docker compose exec redis redis-cli ping          # PONG
docker compose exec redis redis-cli llen arq:queue
```

**Prove the worker is consuming the queue** (this is what `worker_ping` exists for):

```bash
docker compose exec api python -c "
import asyncio
from arq import create_pool
from arq.connections import RedisSettings
from app.config import get_settings

async def main():
    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    job = await pool.enqueue_job('worker_ping')
    print(await job.result(timeout=15))

asyncio.run(main())
"
```

A result means the queue is healthy and the problem is the specific task. A timeout means the
worker is not consuming — restart it: `docker compose restart worker`.

**A document is stuck on `pending`.** The enqueue failed (Redis was down when it was uploaded).
The file is safe; re-drive it:

```bash
docker compose exec api python -c "
import asyncio, sys
from arq import create_pool
from arq.connections import RedisSettings
from app.config import get_settings

async def main(doc_id):
    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    await pool.enqueue_job('embed_document', doc_id)
    print('re-queued', doc_id)

asyncio.run(main(sys.argv[1]))
" <document-id>
```

**A document reads `failed`.** Its reason is in the `error` column and in
`GET /api/knowledge/documents`. Usually: a scanned PDF with no text layer (it needs OCR first), or
an encrypted PDF.

---

## 5. Cost

The `api_usage` table is the ledger; `/metrics` exposes the same numbers to Prometheus.

```sql
-- today, by model and purpose
SELECT model, purpose, sum(tokens_in) AS in, sum(tokens_out) AS out,
       round(sum(cost_usd), 4) AS usd, count(*) AS calls
FROM api_usage
WHERE ts >= date_trunc('day', now())
GROUP BY 1, 2 ORDER BY usd DESC;
```

```promql
sum by (model, purpose) (rate(danah_llm_cost_usd_total[1h])) * 3600   # USD/hour
```

**Cost is spiking.** In order:

1. `PIPELINE_TOKEN_BUDGET` (default 400k tokens) caps a *single run*. A run that hits it stops and
   reports `partial` — check `pipeline_runs.stats.stopped_early`.
2. `DAILY_COST_ALERT_USD` notifies admins; it does **not** stop spending.
3. The usual cause is a source that started flooding. Check `GET /api/items` volume by source and
   disable the offender: `PATCH /api/sources/{id} {"enabled": false}`.
4. Lower `PIPELINE_MAX_ITEMS_PER_RUN`, or raise `SIGNAL_RELEVANCE_THRESHOLD` so the Signal Agent
   archives more noise before the expensive agents see it.

**Emergency stop:** `docker compose stop scheduler`. Cron stops firing; the API stays up. Nothing
is lost — the next scheduled run simply starts from the accumulated items.

---

## 6. The audit chain

```bash
curl -s localhost:8000/api/audit/verify -H "Authorization: Bearer $ADMIN" | jq
```

```json
{ "valid": true, "entries_checked": 1284, "broken_at_id": null }
```

**If `valid: false` — treat it as a security incident.**

`broken_at_id` names the first entry that fails verification: the row that was altered, or the row
following one that was deleted. The application *cannot* produce this state — a database trigger
rejects `UPDATE`, `DELETE` and `TRUNCATE` on `audit_log`. A broken chain therefore means someone
with database-superuser access disabled that trigger.

1. Do not restart anything. Do not run migrations.
2. Snapshot the database immediately (`pg_dump`).
3. Preserve `entry_hash` / `prev_hash` around `broken_at_id` — they are the evidence.
4. Escalate. Rotate the database credentials.

---

## 7. Rate limits

Login `RATE_LIMIT_LOGIN_PER_MINUTE` per IP; chat `RATE_LIMIT_CHAT_PER_MINUTE` per user. A 429
carries `Retry-After`.

The limiter **fails open**: if Redis is unreachable, requests are allowed and
`rate_limit_unavailable` is logged. That is deliberate — a rate limiter is a guardrail, not an
authentication boundary, and a Redis outage must not lock a ministry out of its platform during an
incident. If you see that log line, fix Redis (§4); the platform is temporarily unprotected against
brute force.

---

## 8. Rotating secrets

**`JWT_SECRET_KEY`.** Rotating it invalidates every access token immediately (users re-login) but
**not** refresh tokens, which are opaque and stored hashed. To force a full re-authentication, also
clear the refresh tokens:

```sql
UPDATE refresh_tokens SET revoked_at = now() WHERE revoked_at IS NULL;
```

`app/security/jwt.py::_key_for` is the single seam for a proper keyring (tokens already carry a
`kid` header) if you need overlap-window rotation with no user impact.

**Provider keys.** Update `.env`, `docker compose up -d --build`. No data change; in-flight calls
fail once and retry.

**Admin password.** Change it in the UI. `ADMIN_INITIAL_PASSWORD` is only read by `scripts/seed.py`
when creating the user.

---

## 9. Changing the embedding model

> ⚠️ **This requires a full reindex.** Vectors from different models are not comparable. Changing
> `EMBEDDING_MODEL` without reindexing does not error — it silently degrades every retrieval, which
> is worse.

1. Stop the worker: `docker compose stop worker`.
2. Set `EMBEDDING_MODEL` (and `EMBEDDING_DIM` if the dimension changes) in `.env`.
3. If `EMBEDDING_DIM` changed, the `vector(n)` column must be recreated — write a new Alembic
   migration altering the column type, or (in development) `docker compose down -v` and re-migrate.
4. Re-embed the corpus:

```bash
docker compose exec api python -c "
import asyncio
from app.db import get_session_factory
from app.services.rag.indexer import reindex_all

async def main():
    async with get_session_factory()() as s:
        for r in await reindex_all(s):
            print(r.document_id, r.status.value, r.chunk_count)
        await s.commit()

asyncio.run(main())
"
```

5. `docker compose start worker`.

---

## 10. Load test

```bash
make loadtest                                     # dashboard only; free
python -m scripts.loadtest --users 50 --requests 20
python -m scripts.loadtest --endpoint chat --users 5 --requests 2   # costs money
```

Reports p50/p95/p99 — a mean would hide the tail that users actually notice.

**Baseline** (dev laptop; Docker Desktop; Postgres, Redis and the API sharing one machine; fake
LLM gateway so the provider's latency is excluded):

| Endpoint | Load | p50 | p95 | p99 | Notes |
|---|---|---|---|---|---|
| `GET /api/dashboard/summary` | 20 users × 10 req | ~35 ms | ~90 ms | ~140 ms | aggregate queries, no N+1 |
| `POST /api/agent/chat` | 5 users × 2 req | provider-bound | — | — | latency is the model's, not DANAH's |

Re-run this against the real deployment and record the numbers — a baseline from a laptop is a
sanity check, not a capacity plan. 429s under a burst are the rate limiter working, not a failure.

---

## 11. Restoring from nothing

```bash
git clone <repo> && cd danah
cp .env.example .env          # set JWT_SECRET_KEY, ADMIN_INITIAL_PASSWORD, provider keys
docker compose up -d          # api runs `alembic upgrade head` on start
docker compose exec -T postgres pg_restore -U danah -d danah --clean --if-exists < backup.dump
# restore data/documents/ from its own backup
curl -s localhost:8000/api/healthz | jq
curl -s localhost:8000/api/audit/verify -H "Authorization: Bearer $ADMIN" | jq   # chain must still verify
```

A restored database whose audit chain still verifies is a restored database you can trust.
