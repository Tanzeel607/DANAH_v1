# DANAH — Strategic Intelligence Platform (Backend)

Enterprise backend for a government ministry's strategic intelligence platform: continuous
ingestion of external signals, a pipeline of six specialised AI agents, retrieval-grounded chat
with citations, a human approval gate on every publication, and a hash-chained audit trail.

> **Grounded or silent · Human in the loop · Sovereign by default · Audit everything · Bilingual first-class**

The v11 HTML front end (`DANAH_Strategic_Intelligence_Platform_v11.html`) is the existing UI
prototype this backend was designed for. It is **read-only reference** — this repository does not
modify or rebuild it.

---

## Quick start

```bash
cp .env.example .env          # then set JWT_SECRET_KEY + provider keys
docker compose up             # api, worker, scheduler, postgres+pgvector, redis
docker compose exec api python -m scripts.seed
curl localhost:8000/api/healthz
```

Interactive API docs: <http://localhost:8000/docs>

**No API keys yet?** The stack still builds, boots and passes its full test suite — LLM-backed
routes return a clear `503 llm_not_configured` instead of faking answers. See
[`FIRST_RUN.md`](FIRST_RUN.md) for the exact commands to run once keys are added.

---

## Architecture at a glance

| Layer | Technology | Responsibility |
|---|---|---|
| API | FastAPI + Pydantic v2 | The only entry point. Enforces auth, RBAC, classification. |
| Services | auth · RAG · LLM gateway · 6 agents · orchestrator · ingestion · approvals · memory · audit · notifications | All business logic; the API layer stays thin. |
| Workers | ARQ worker + cron scheduler | Source polling, embeddings, pipeline runs, daily briefing. |
| Data | PostgreSQL 16 + pgvector + FTS, Redis 7 | One relational source of truth; vectors co-located for sovereignty. |

**The six agents:** Signal (triage) → Risk ∥ Opportunity ∥ Policy (parallel analysis) →
Briefing (EN + AR) → Memory (durable lessons). Every output lands in the approvals queue as
`pending_approval`; only a human decision publishes it.

Full technical reference: [`DANAH_DEVELOPER_ARCHITECTURE.md`](DANAH_DEVELOPER_ARCHITECTURE.md) ·
endpoint contract: [`docs/API.md`](docs/API.md) · operations: [`docs/RUNBOOK.md`](docs/RUNBOOK.md) ·
engineering decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## Development

Requires Python 3.12 and Docker.

```bash
make venv install     # 3.12 virtualenv + dependencies
docker compose up -d postgres redis
make migrate seed
make dev              # uvicorn with autoreload
make worker           # ARQ worker (separate shell)
```

Windows without GNU Make: `./make.ps1 <target>` exposes identical targets.

### Quality gates

```bash
make check            # ruff + mypy --strict + pytest — all must be green
```

`mypy --strict` passes over `app/`. No LLM is ever called in tests: the fake gateway fixture in
`tests/conftest.py` substitutes at the gateway interface, so the real code paths are exercised.

---

## Security posture

- Argon2 password hashing; JWT access (15 min) + rotating refresh (14 d, hashed at rest).
- **Classification is enforced in SQL**, not in prompts — an over-classified chunk cannot reach the
  model's context at all (`PUBLIC < INTERNAL < OFFICIAL < OFFICIAL_SENSITIVE`).
- Role → clearance ceiling: `viewer → INTERNAL`, `analyst → OFFICIAL`, `executive`/`admin` → `OFFICIAL_SENSITIVE`.
- Append-only, hash-chained `audit_log` (`entry_hash = sha256(prev_hash + canonical_json(row))`);
  UPDATE/DELETE blocked by a database trigger. `GET /api/audit/verify` re-walks the chain.
- Redis sliding-window rate limits on login and chat; per-source HMAC on webhook ingestion.
- Document text is never logged at OFFICIAL or above.

⚠️ `CORS_ORIGINS` ships with `null` so the v11 HTML file can be opened from disk during development.
**Remove it in production** — the config layer refuses to start if `APP_ENV=production` and `null`
is still present.
