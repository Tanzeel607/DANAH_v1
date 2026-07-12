# ============================================================================
# DANAH — developer task runner
# ----------------------------------------------------------------------------
# Bare-metal dev assumes a Python 3.12 virtualenv at ./.venv and a running
# Postgres+Redis (`make up` starts just those two from docker-compose).
# Windows users without GNU Make: use ./make.ps1 <target> (identical targets).
# ============================================================================

.DEFAULT_GOAL := help
.PHONY: help install venv dev up down logs lint format typecheck test test-cov \
        migrate migration downgrade seed smoke loadtest worker scheduler check clean

PY        ?= python
VENV      := .venv
ifeq ($(OS),Windows_NT)
BIN       := $(VENV)/Scripts
else
BIN       := $(VENV)/bin
endif
APP       := app
COMPOSE   := docker compose

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv: ## Create the Python 3.12 virtualenv
	uv venv --python 3.12 $(VENV)

install: ## Install runtime + dev dependencies into the venv
	uv pip install --python $(BIN)/python -e ".[dev]"

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
dev: ## Run the API with autoreload (bare metal)
	$(BIN)/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

worker: ## Run the ARQ worker (bare metal)
	$(BIN)/arq app.workers.worker.WorkerSettings

scheduler: ## Run the ARQ cron scheduler (bare metal)
	$(BIN)/arq app.workers.worker.SchedulerSettings

up: ## Start the full stack in Docker (api, worker, scheduler, postgres, redis)
	$(COMPOSE) up -d --build

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down

logs: ## Tail the API logs
	$(COMPOSE) logs -f api

# ---------------------------------------------------------------------------
# Quality gates — all three must be green before any phase advances
# ---------------------------------------------------------------------------
lint: ## ruff check + format check
	$(BIN)/ruff check $(APP) tests scripts
	$(BIN)/ruff format --check $(APP) tests scripts

format: ## Auto-fix lint + format
	$(BIN)/ruff check --fix $(APP) tests scripts
	$(BIN)/ruff format $(APP) tests scripts

typecheck: ## mypy --strict
	$(BIN)/mypy --strict $(APP)

test: ## Full pytest suite
	$(BIN)/pytest

test-cov: ## Test suite with coverage report
	$(BIN)/pytest --cov=app --cov-report=term-missing --cov-report=html

check: lint typecheck test ## The phase gate: lint + typecheck + test

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
migrate: ## Apply all Alembic migrations
	$(BIN)/alembic upgrade head

migration: ## Autogenerate a migration: make migration m="add x"
	$(BIN)/alembic revision --autogenerate -m "$(m)"

downgrade: ## Roll back one migration
	$(BIN)/alembic downgrade -1

seed: ## Seed roles, admin user, default sources, sample documents
	$(BIN)/python -m scripts.seed

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
smoke: ## Live end-to-end acceptance check (requires real API keys in .env)
	$(BIN)/python -m scripts.smoke_test

loadtest: ## Async burst load test against chat + dashboard
	$(BIN)/python -m scripts.loadtest

clean: ## Remove caches and build artefacts
	rm -rf .mypy_cache .pytest_cache .ruff_cache htmlcov .coverage dist build
