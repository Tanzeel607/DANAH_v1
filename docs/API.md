# DANAH — API Reference

Base URL `/api` · JSON · Bearer JWT unless marked **public**.
Interactive docs: <http://localhost:8000/docs> · OpenAPI: `/openapi.json`

This document is the human-readable contract. It matches master prompt §7.7 exactly.
**Endpoints are never renamed after Phase 1 without an entry in the changelog at the bottom.**

---

## Conventions

**Errors.** Every failure returns the same envelope. Stack traces are never returned.

```json
{ "error": { "code": "permission_denied", "message": "…", "request_id": "8f3c…" } }
```

| Status | `code` | Meaning |
|---|---|---|
| 401 | `auth_error` | Missing, invalid or expired token |
| 403 | `permission_denied` | Role or clearance insufficient |
| 404 | `not_found` | No such resource *(also returned instead of 403 when confirming existence would leak)* |
| 409 | `conflict` / `approval_error` | Conflicts with current state |
| 422 | `validation_error` | Input failed validation; `fields[]` names what |
| 429 | `rate_limited` | Rate limit exceeded; `Retry-After` header set |
| 502 | `llm_gateway_error` | Provider unreachable after retries |
| 503 | `llm_not_configured` | No provider key — see `FIRST_RUN.md` |

**Request ids.** Send `X-Request-ID` to correlate your logs with ours; it is echoed on every
response and appears in every log line and audit entry the request produces.

**Roles and clearance.** Authorisation is enforced server-side, per endpoint. Clearance is
applied as a SQL filter on reads — content above your clearance is not filtered *out* of your
results, it is never read at all.

| Role | Clearance ceiling | Can |
|---|---|---|
| `viewer` | `INTERNAL` | read published outputs |
| `analyst` | `OFFICIAL` | + upload docs, sync sources, trigger runs, full chat |
| `executive` | `OFFICIAL_SENSITIVE` | + approve/reject, generate briefings |
| `admin` | `OFFICIAL_SENSITIVE` | + manage users/sources, read the audit trail |

**Pagination.** List endpoints that can grow without bound take `limit` (1–200, default 50) and
`offset`, and return `{items, total, limit, offset}`.

---

## 1. Auth

### `POST /api/auth/login` — public
```json
{ "email": "admin@ministry.gov", "password": "…" }
```
→ `200`
```json
{ "access_token": "eyJ…", "refresh_token": "…", "token_type": "bearer", "expires_in": 900 }
```
An unknown email and a wrong password are indistinguishable, by design. Rate limited to
`RATE_LIMIT_LOGIN_PER_MINUTE` per IP.

### `POST /api/auth/refresh` — public
```json
{ "refresh_token": "…" }
```
→ `200` a **new** pair. Refresh tokens rotate: the presented token is revoked immediately.

> **Reuse detection.** Presenting an already-revoked refresh token is the signature of a stolen
> token being replayed. DANAH revokes the user's entire token family; the legitimate user
> re-authenticates and the thief's copy dies with it.

### `GET /api/auth/me`
→ `200 UserOut` — includes `clearance`, derived from the role. Never sent by the client.

---

## 2. Chat (grounded)

### `POST /api/agent/chat`
```json
{ "session_id": "uuid | null", "message": "What is the non-oil GDP target?", "language": "en | ar | null" }
```
→ `200`
```json
{
  "session_id": "…", "message_id": "…",
  "answer": "The Ministry targets a non-oil GDP share of 65 percent by 2030 [1].",
  "citations": [
    { "n": 1, "kind": "chunk", "id": "…", "document_id": "…",
      "title": "National Economic Diversification Strategy 2030",
      "snippet": "…", "score": 0.87 }
  ],
  "confidence": 0.82, "grounded": true, "language": "en",
  "latency_ms": 2140, "tokens_in": 1832, "tokens_out": 96
}
```

- `citations[].n` matches the `[n]` markers in `answer` — hyperlink them inline.
- **Only sources the model actually cited are returned.** A retrieved-but-uncited chunk is not a
  citation.
- `grounded: false` + `confidence: 0` is an **abstention**: the corpus did not support an answer
  and the assistant said so. Render it as an honest "not in my sources", not as a failure.
- Retrieval is bounded by the caller's clearance. Two users asking the same question can
  legitimately get different answers.
- Omit `language` and the answer comes back in the language of the question.

### `GET /api/agent/chat/sessions` → `200 ChatSessionOut[]`
### `GET /api/agent/chat/sessions/{id}` → `200 ChatSessionDetail` (full transcript)

---

## 3. Knowledge base

### `POST /api/knowledge/documents` — **analyst+** · `multipart/form-data`
| field | type | notes |
|---|---|---|
| `file` | file | pdf, docx, txt, md, html · ≤ `MAX_UPLOAD_SIZE_MB` |
| `title` | string? | defaults to the filename |
| `classification` | enum | default `INTERNAL`; **cannot exceed your own clearance** |

→ `202` `{ id, title, filename, status: "pending" }`

Indexing (extract → chunk → embed) runs in the background. Poll the list endpoint until
`status` is `indexed`. A document that cannot be read ends `failed` with the reason in `error`
(e.g. a scanned PDF needs OCR first).

### `GET /api/knowledge/documents?status=&limit=&offset=` → `200 DocumentOut[]`
Only documents at or below your clearance.

### `POST /api/knowledge/search` — **analyst+**
```json
{ "query": "data residency", "k": 8, "language": null, "hybrid": null }
```
→ `200` `{ query, hits[], total, hybrid }` — each hit carries `score` (fused), and
`vector_score` / `keyword_score` so you can see which arm found it.

Hybrid = pgvector cosine **+** Postgres full-text, fused by reciprocal rank. The keyword arm is
what finds exact identifiers ("NY.GDP.MKTP.KD.ZG", "Article 14(b)") that embeddings miss.

---

## 4. Sources & items

| Method | Path | Role | Purpose |
|---|---|---|---|
| `GET` | `/api/sources` | any | list + health (`healthy` / `stale` / `failing` / `disabled`) |
| `POST` | `/api/sources` | admin | create |
| `PATCH` | `/api/sources/{id}` | admin | update |
| `POST` | `/api/sources/{id}/sync` | analyst+ | sync now → `{fetched, created, duplicates, status}` |
| `GET` | `/api/items` | any | filters: `status`, `category`, `urgency`, `source_id`, `q`, `from`, `to`, `limit`, `offset` |
| `GET` | `/api/items/{id}` | any | detail incl. raw payload |
| `POST` | `/api/ingest/webhook/{source_id}` | **HMAC** | push ingestion |

Items are deduplicated on `dedup_hash = sha256(source_id + external_id | url | title+date)`, so
re-polling a source never duplicates. `triage` (relevance, category, urgency, rationale) is
filled by the Signal Agent; it is also flattened onto the item so the UI can sort on it directly.

**Webhook auth.** Sign the raw body with the source's HMAC secret and send
`X-DANAH-Signature: sha256=<hex>`. Compared in constant time. This is the seam through which a
licensed feed (Bloomberg, Reuters) arrives later with **no new code path**.

---

## 5. Pipeline

### `POST /api/pipeline/run` — **analyst+**
```json
{ "max_items": 150, "agents": null }
```
→ `202` `{ run_id, status: "running" }` — poll the detail endpoint.

### `GET /api/pipeline/runs` → `200` paginated
### `GET /api/pipeline/runs/{id}` → `200 PipelineRunDetail`

The live view the UI polls. Each step carries its own ledger:

```json
{
  "id": "…", "status": "completed", "total_tokens": 48213, "total_cost_usd": 0.213,
  "steps": [
    { "agent": "signal", "status": "completed", "tokens_in": 8210, "tokens_out": 640,
      "cost_usd": 0.011, "latency_ms": 4120 },
    { "agent": "risk", "status": "completed", "tokens_in": 14022, "tokens_out": 2180,
      "cost_usd": 0.075, "latency_ms": 11800 }
  ]
}
```

Order: **Signal → (Risk ∥ Opportunity ∥ Policy) → Briefing → Memory.** A failed step marks the
run `partial`; steps that do not depend on it still run.

---

## 6. Insights

### `GET /api/insights` — filters `kind`, `status`, `min_severity`, `domain`, `run_id`, `q`
Viewers see **published only**. `severity` is 1–5 (it carries *impact* for opportunities;
`impact` is mirrored in the response so the UI need not branch).

### `GET /api/insights/{id}` → detail incl. citations, recommendations, and — for `kind=policy` —
a `policy` block (`what_changed`, `jurisdictions`, `compliance_impact`, `required_response`,
`deadline`).

---

## 7. Briefings

| Method | Path | Role |
|---|---|---|
| `GET` | `/api/briefings` | any |
| `GET` | `/api/briefings/{id}` | any |
| `POST` | `/api/briefings/generate` | **executive+** |

Detail carries `body_en` **and** `body_ar`. Arabic is a first-class product requirement produced
by a dedicated second LLM pass, not a machine-translation afterthought — it is never empty.

---

## 8. Approvals — the publication gate

**Nothing an agent writes is ever published automatically.** Every insight and briefing is created
as a draft and immediately enters this queue as `pending_approval`.

### `GET /api/approvals?status=pending` — **executive+**
Rows are denormalised (`subject_title`, `subject_summary`, `subject_confidence`,
`subject_severity`) so the queue renders without an N+1 fetch.

### `POST /api/approvals/{id}/decision` — **executive+**
```json
{ "decision": "approved | rejected | changes_requested", "comment": "…" }
```
→ `200` `{ id, status, subject_type, subject_id, subject_status, decided_by, decided_at }`

- `approved` → subject becomes `published` (visible to `viewer`)
- `rejected` → subject becomes `rejected` (hidden)
- `changes_requested` → stays out of sight; the comment goes back to the agent's authors

Every decision is written to the hash-chained audit log with the deciding user and their IP.

---

## 9. Dashboard

### `GET /api/dashboard/summary`
One call fills the entire command centre: `counts`, `latest_run`, `latest_briefing`,
`top_insights`, `source_health`, `cost` (today / 7-day / threshold / over-threshold), and `kpi`.

---

## 10. Memory · Notifications · Audit · Ops

| Method | Path | Role | Purpose |
|---|---|---|---|
| `GET` | `/api/memory` | analyst+ | institutional memory entries |
| `POST` | `/api/memory/search` | analyst+ | semantic search over memory |
| `GET` | `/api/notifications` | any | in-app notifications |
| `POST` | `/api/notifications/read` | any | mark read (empty `ids` = mark all) |
| `GET` | `/api/audit` | **admin** | audit trail, filterable |
| `GET` | `/api/audit/verify` | **admin** | re-walk the hash chain |
| `GET` | `/api/healthz` | public | liveness + dependency status |
| `GET` | `/metrics` | public | Prometheus |

### `GET /api/audit/verify`
```json
{ "valid": true, "entries_checked": 1284, "broken_at_id": null, "first_id": 1, "last_id": 1284 }
```
Re-computes `entry_hash = sha256(prev_hash + canonical_json(row))` for every entry. If a row was
altered or deleted, `valid` is `false` and `broken_at_id` names **the first entry that fails** —
i.e. the tampered row, or the row after the gap.

The database itself refuses to help an attacker: a trigger rejects `UPDATE`, `DELETE` and
`TRUNCATE` on `audit_log`. Only a superuser disabling that trigger can alter history — and this
endpoint is what detects it.

---

## Front-end integration notes

- `CORS_ORIGINS` ships with `null` so the v11 HTML file can be opened from disk in development.
  **Remove it in production** — the config layer refuses to boot with `null` when
  `APP_ENV=production`.
- Responses are flat and display-ready: ids, ISO-8601 timestamps, and pre-computed fields
  (`health`, `impact`, `total_cost_usd`, `relevance`) so the UI performs no arithmetic.
- `GET /api/dashboard/summary` is the single call behind the command centre.

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-13 | Phase 1 — auth, chat, knowledge endpoints published. Initial contract. |
| 2026-07-13 | Phase 2 — sources, items, pipeline, insights, dashboard. |
| 2026-07-13 | Phase 3 — briefings, approvals, memory, notifications. |
| 2026-07-13 | Phase 4 — audit + verify, webhook ingestion, admin. No prior endpoint renamed. |
