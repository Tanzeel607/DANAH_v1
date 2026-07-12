# DANAH — Decision Log

Every non-obvious engineering decision taken during the autonomous build. Format:
**decision — reason — alternative rejected.**

Decisions 1–7 are inherited from `DANAH_DEVELOPER_ARCHITECTURE.md` §12 and are restated here so this file
is the single operational record.

---

## Inherited architecture decisions

| # | Decision | Reason | Alternative rejected |
|---|---|---|---|
| 1 | Python 3.12 + FastAPI | AI ecosystem, async, typing, OpenAPI out of the box | Node/Nest — weaker AI tooling |
| 2 | pgvector inside the primary Postgres | sovereignty, one backup + one security boundary, transactional joins between chunks and metadata | Pinecone/Weaviate — external SaaS, data-residency risk |
| 3 | Custom thin agent framework | full control of prompts/logging/audit, fewer deps, easier government review | LangChain/LlamaIndex — abstraction churn, harder to audit |
| 4 | ARQ for background jobs | async-native, matches FastAPI stack, cron built in, tiny ops footprint | Celery — sync-first, heavier |
| 5 | Anthropic primary behind a provider-agnostic gateway | quality + tool use; gateway keeps switching cost near zero | hard-coding one vendor |
| 6 | Human approval on every publication | government accountability requirement | auto-publish with review-after |
| 7 | Hash-chained audit in Postgres | tamper-evidence without new infrastructure | external ledger — overkill at this stage |

---

## Build-time decisions

### 8 — Python 3.12.13 provisioned via `uv`, not the system 3.14
**Reason:** the host's default interpreter is 3.14.3. The locked stack (`asyncpg`, `pgvector`, `argon2-cffi`)
does not yet publish wheels for 3.14 across the board, and the master prompt specifies Python 3.12+. `uv` installs a
managed 3.12.13 toolchain in userspace with no admin rights and no interference with the system Python.
**Alternative rejected:** building on 3.14 and compiling C extensions from source — fragile on Windows, and it
would diverge from the `python:3.12-slim` runtime used in the Docker image.

### 9 — GNU Make installed via `winget` (ezwinports.make 4.4.1); `make.ps1` added alongside the `Makefile`
**Reason:** the master prompt requires a `Makefile` (`make dev/test/lint/migrate/seed`). Make is not native to
Windows. The `Makefile` is the canonical, CI/Linux-facing deliverable; `make.ps1` mirrors its targets so a Windows
developer with no Make installed can run the same commands.
**Alternative rejected:** replacing Make with a task runner (`just`, `invoke`) — the master prompt locks the Makefile.

### 10 — Repository root is the existing project folder
**Reason:** the execution prompt requires `PROGRESS.md` and `BUILD_REPORT.md` "at the repo root" alongside the spec
documents and the v11 HTML prototype it references. `§5`'s `danah/` denotes the repository itself, not a nested folder.
**Alternative rejected:** nesting the code in `danah/` — would place the HTML prototype and specs outside the repo.

### 11 — Local `.env` generated with random secrets, inline comments stripped
**Reason:** `python-dotenv` tolerates trailing `# comments`, but stripping them removes any ambiguity for values that
legitimately contain spaces (e.g. `PIPELINE_SCHEDULE_CRON=0 5 * * *`). `JWT_SECRET_KEY`, `ADMIN_INITIAL_PASSWORD`
and `WEBHOOK_HMAC_DEFAULT_SECRET` are randomly generated at build time and never printed or committed.
**Alternative rejected:** committing a `.env` with placeholder secrets — forbidden by master prompt §12.

### 12 — `uuid4` primary keys (not `uuid7`)
**Reason:** master prompt §6 says "uuid7 if available, else uuid4". Python 3.12's stdlib `uuid` has no `uuid7`
(added in 3.14), and adding a third-party `uuid7` package for key generation is not worth a new dependency in a
government-review context. Postgres `gen_random_uuid()` (pgcrypto/pg16 builtin) is the DB-side default.
**Alternative rejected:** `uuid6` PyPI package — extra unaudited dependency for marginal index-locality gain.

### 13 — Tokenizer for chunk sizing: `tiktoken` `cl100k_base` as a provider-neutral token estimator
**Reason:** chunking targets ~800 tokens with 150 overlap. The embedding provider may be Voyage or OpenAI, and
Anthropic does not ship a local tokenizer. `tiktoken` gives a fast, deterministic, offline token count that is
within a few percent for all three providers — good enough for chunk sizing, which is not a correctness boundary.
**Alternative rejected:** provider-specific remote token-counting endpoints — network round-trip per chunk, and
unavailable offline/in tests.

### 14 — Test database: real Postgres+pgvector via docker-compose, not SQLite
**Reason:** the schema depends on pgvector (`vector` columns, HNSW index), Postgres FTS (`tsvector`), `jsonb`,
`text[]`, `bigserial`, and an append-only trigger. None of these exist in SQLite, so a SQLite test DB would test a
different schema than production. Tests run against a dedicated `danah_test` database created and migrated per session.
**Alternative rejected:** SQLite/aiosqlite in tests — would force the retriever and audit trigger to be stubbed,
violating "never replace a real implementation with a stub to get green".

### 15 — Classification filtering is applied in SQL, never in the prompt
**Reason:** architecture §5 requires that leakage be *structurally* impossible. The retriever binds the caller's
clearance ceiling into the `WHERE` clause, so an over-cleared chunk can never reach the LLM context in the first place.
**Alternative rejected:** post-filtering results after retrieval, or instructing the model to ignore over-classified
content — both leave the sensitive text in memory/logs and depend on model compliance.

### 16 — Compose host ports are configurable (`POSTGRES_HOST_PORT` / `REDIS_HOST_PORT`)
**Reason:** an unrelated container on this machine (`crm_postgres`) already owned host port 5432. Docker
started DANAH's Postgres *without* a host binding, so `localhost:5432` silently resolved to the other
project's database and every connection failed with `password authentication failed for user "danah"`. The
compose file now publishes `${POSTGRES_HOST_PORT:-5432}:5432`, so the shipped default still matches the
master prompt's `.env.example`, while a developer whose 5432 is occupied changes one variable. This repo's
local `.env` uses 5433. Container-internal ports are unchanged (services talk to `postgres:5432`).
**Alternative rejected:** stopping the other project's container — destructive, and not this build's call.

### 17 — Generated secrets exclude `$` and `#`
**Reason:** Docker Compose performs variable interpolation on `.env` values, so a `$` inside a generated
password is parsed as `${VAR}` and silently expands to an empty string — the value inside the container
would differ from the value on disk. `#` risks being read as a comment by strict `.env` parsers.
Generated secrets therefore draw from an alphabet excluding `$ # " ' \`.
**Alternative rejected:** escaping `$` as `$$` — works for Compose but corrupts the value for every other
consumer of the file.

### 19 — Metrics built on `prometheus_client` directly, not `prometheus-fastapi-instrumentator`
**Reason:** the stack table (§4) names `prometheus-fastapi-instrumentator`, but version 7.1.0 (its
latest) is broken against Starlette 0.52, which FastAPI 0.139 requires: the instrumentator reads
`route.path` while resolving a request, and Starlette's new `_IncludedRouter` object has no `path`
attribute, so **every request raises `AttributeError`**. Master prompt §4 permits the closest
maintained equivalent when a named library is unavailable. `prometheus_client` is the official
Prometheus Python client, is already a transitive dependency, and `app/metrics.py` is ~150 lines.
It also gives what the wrapper never could: DANAH's own `danah_llm_cost_usd_total` /
`danah_llm_tokens_total` counters, which the Phase-4 acceptance criteria require. Route templates
(not concrete URLs) are used as labels, so record ids never inflate metric cardinality.
**Alternative rejected:** pinning Starlette below 0.52 — would drag FastAPI back with it and pin the
whole stack to an ageing release to satisfy one optional wrapper.

### 20 — `PENDING-CREDENTIALS` build mode
**Reason:** no `ANTHROPIC_API_KEY` / `VOYAGE_API_KEY` / `OPENAI_API_KEY` was present at build time. Per execution
prompt Rule 8, all production code paths are built for real and every test passes against `FakeLLMGateway` /
`FakeEmbedder`. Acceptance criteria that can only be proven with a live provider are marked `PENDING-CREDENTIALS`
in `PROGRESS.md` / `BUILD_REPORT.md` and are executable by the user through `scripts/smoke_test.py` (`make smoke`).
**Alternative rejected:** inventing placeholder keys or stubbing production code to force a green "live" check —
explicitly forbidden.
