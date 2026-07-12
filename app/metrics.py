"""Prometheus metrics.

Built directly on `prometheus_client` rather than `prometheus-fastapi-instrumentator`, which
is incompatible with Starlette 0.52 (it reads `route.path` on `_IncludedRouter`, which has no
such attribute). See docs/DECISIONS.md #19.

Two families:
  * **HTTP** — request count and latency, labelled by method / templated path / status class.
    The path is the *route template* (`/api/insights/{insight_id}`), never the concrete URL, so
    ids never become metric labels and cardinality stays bounded.
  * **DANAH domain** — LLM tokens and cost by provider/model/purpose, plus pipeline and
    approval counters. These are what make the cost ledger visible in Grafana
    (master prompt §10, Phase 4).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import generate_latest as _generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

REGISTRY = CollectorRegistry(auto_describe=True)

# --- HTTP -------------------------------------------------------------------
HTTP_REQUESTS = Counter(
    "danah_http_requests_total",
    "Total HTTP requests.",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)
HTTP_ERRORS = Counter(
    "danah_http_errors_total",
    "HTTP responses with a 4xx or 5xx status.",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "danah_http_request_duration_seconds",
    "HTTP request latency.",
    labelnames=("method", "path"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)
HTTP_IN_PROGRESS = Gauge(
    "danah_http_requests_in_progress",
    "HTTP requests currently being served.",
    registry=REGISTRY,
)

# --- LLM cost ledger --------------------------------------------------------
LLM_TOKENS = Counter(
    "danah_llm_tokens_total",
    "LLM tokens consumed.",
    labelnames=("provider", "model", "purpose", "direction"),
    registry=REGISTRY,
)
LLM_COST_USD = Counter(
    "danah_llm_cost_usd_total",
    "LLM spend in USD.",
    labelnames=("provider", "model", "purpose"),
    registry=REGISTRY,
)
LLM_CALLS = Counter(
    "danah_llm_calls_total",
    "LLM provider calls.",
    labelnames=("provider", "model", "purpose", "outcome"),
    registry=REGISTRY,
)
LLM_LATENCY = Histogram(
    "danah_llm_latency_seconds",
    "LLM provider call latency.",
    labelnames=("provider", "model", "purpose"),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 90.0),
    registry=REGISTRY,
)

# --- Domain -----------------------------------------------------------------
PIPELINE_RUNS = Counter(
    "danah_pipeline_runs_total",
    "Completed pipeline runs by final status.",
    labelnames=("trigger", "status"),
    registry=REGISTRY,
)
AGENT_STEPS = Counter(
    "danah_agent_steps_total",
    "Agent step executions by outcome.",
    labelnames=("agent", "status"),
    registry=REGISTRY,
)
INSIGHTS_CREATED = Counter(
    "danah_insights_created_total",
    "Insights created by the agents (all enter the approval queue as drafts).",
    labelnames=("kind",),
    registry=REGISTRY,
)
APPROVAL_DECISIONS = Counter(
    "danah_approval_decisions_total",
    "Human approval decisions.",
    labelnames=("subject_type", "decision"),
    registry=REGISTRY,
)
INGESTED_ITEMS = Counter(
    "danah_ingested_items_total",
    "Items persisted by the ingestion connectors (after deduplication).",
    labelnames=("connector",),
    registry=REGISTRY,
)
RATE_LIMITED = Counter(
    "danah_rate_limited_total",
    "Requests rejected by the rate limiter.",
    labelnames=("scope",),
    registry=REGISTRY,
)


def record_llm_call(
    *,
    provider: str,
    model: str,
    purpose: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    latency_ms: int,
    outcome: str = "success",
) -> None:
    """Single funnel for every provider call — mirrors the `api_usage` row written alongside."""
    LLM_CALLS.labels(provider=provider, model=model, purpose=purpose, outcome=outcome).inc()
    if outcome != "success":
        return
    LLM_TOKENS.labels(provider=provider, model=model, purpose=purpose, direction="in").inc(
        tokens_in
    )
    LLM_TOKENS.labels(provider=provider, model=model, purpose=purpose, direction="out").inc(
        tokens_out
    )
    LLM_COST_USD.labels(provider=provider, model=model, purpose=purpose).inc(cost_usd)
    LLM_LATENCY.labels(provider=provider, model=model, purpose=purpose).observe(latency_ms / 1000.0)


def generate_latest() -> bytes:
    return _generate_latest(REGISTRY)


def _route_template(request: Request) -> str:
    """Resolve the concrete URL to its route template, keeping label cardinality bounded.

    `/api/insights/7f3a.../` becomes `/api/insights/{insight_id}`. Unmatched paths collapse to
    `__unmatched__` so a scanner probing random URLs cannot explode the metric space.
    """
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            # Mounts and included routers do not all expose `.path`; skip those rather than
            # assume the attribute exists (this is exactly what broke the instrumentator).
            path = getattr(route, "path", None)
            if isinstance(path, str):
                return path
    return "__unmatched__"


class MetricsMiddleware(BaseHTTPMiddleware):
    """Counts and times every request, including ones that raise."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path == "/metrics":
            return await call_next(request)

        method = request.method
        start = time.perf_counter()
        HTTP_IN_PROGRESS.inc()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            # The global handler renders the 500 body; the metric must still record it.
            path = _route_template(request)
            HTTP_REQUESTS.labels(method=method, path=path, status="500").inc()
            HTTP_ERRORS.labels(method=method, path=path, status="500").inc()
            HTTP_LATENCY.labels(method=method, path=path).observe(time.perf_counter() - start)
            raise
        finally:
            HTTP_IN_PROGRESS.dec()

        path = _route_template(request)
        status = str(status_code)
        HTTP_REQUESTS.labels(method=method, path=path, status=status).inc()
        if status_code >= 400:
            HTTP_ERRORS.labels(method=method, path=path, status=status).inc()
        HTTP_LATENCY.labels(method=method, path=path).observe(time.perf_counter() - start)
        return response


async def metrics_endpoint(_: Request) -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
