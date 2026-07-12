"""Structured JSON logging with a request id propagated through every layer.

The request id is held in a `ContextVar`, so any code — an API handler, a service, an
ARQ task, an LLM call deep in the gateway — can call `get_request_id()` without it being
threaded through function signatures (master prompt §3.7).

Redaction: `redact_text()` is the single place that decides whether document/chat text
may be logged. At OFFICIAL and above it never is (master prompt §12).
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from structlog.types import EventDict, Processor

from app.config import Settings
from app.enums import CLASSIFICATION_RANK, Classification

_request_id: ContextVar[str] = ContextVar("request_id", default="-")

# Keys whose values must never reach a log line, wherever they appear.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "api_key",
        "secret",
        "jwt_secret_key",
        "anthropic_api_key",
        "openai_api_key",
        "voyage_api_key",
        "hmac_secret",
    }
)

_REDACTED = "***REDACTED***"


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str:
    return _request_id.get()


def new_request_id() -> str:
    return str(uuid.uuid4())


def redact_text(text: str, classification: Classification, *, keep: int = 0) -> str:
    """Return text safe to log at the given classification.

    At OFFICIAL and above the content itself is withheld and only its length is reported —
    ids and counts remain loggable, the text does not.
    """
    if CLASSIFICATION_RANK[classification] >= CLASSIFICATION_RANK[Classification.OFFICIAL]:
        return f"<redacted:{classification.value}:{len(text)}chars>"
    if keep and len(text) > keep:
        return text[:keep] + "…"
    return text


def _scrub_sensitive(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Defence in depth: blank out any obviously-secret key that reaches a log call."""
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = _REDACTED
    return event_dict


def _add_request_id(_: Any, __: str, event_dict: EventDict) -> EventDict:
    event_dict["request_id"] = get_request_id()
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Install structlog + stdlib logging. Idempotent; safe to call from worker and API."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_request_id,
        _scrub_sensitive,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Human-readable in dev; strict JSON everywhere else (that is what log shippers parse).
    renderer: Processor = (
        structlog.dev.ConsoleRenderer(colors=False)
        if settings.app_debug and not settings.is_production
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # A stdlib-backed factory (not PrintLoggerFactory): `add_logger_name` reads
        # `logger.name`, which only a stdlib Logger has. It also means library logs and
        # ours share one handler and one stream.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    # uvicorn's own access log duplicates our request middleware; silence it.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
    for noisy in ("httpx", "httpcore", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign/propagate a request id and emit one structured line per request."""

    def __init__(self, app: Any, *, header_name: str = "X-Request-ID") -> None:
        super().__init__(app)
        self.header_name = header_name
        self._log = structlog.get_logger("app.request")

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        import time

        incoming = request.headers.get(self.header_name)
        request_id = incoming or new_request_id()
        set_request_id(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The global handler renders the body; here we only close out the log line.
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            self._log.exception(
                "request_error",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            structlog.contextvars.clear_contextvars()
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        self._log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            client=request.client.host if request.client else None,
        )
        response.headers[self.header_name] = request_id
        structlog.contextvars.clear_contextvars()
        return response


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
