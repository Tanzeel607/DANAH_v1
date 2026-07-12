"""Exception hierarchy and the global handlers that render it.

Clients only ever see `{"error": {"code", "message", "request_id"}}`. Stack traces and
internal detail are logged, never returned (master prompt §3.6).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import get_request_id

log = structlog.get_logger(__name__)

# Starlette renamed HTTP_422_UNPROCESSABLE_ENTITY -> HTTP_422_UNPROCESSABLE_CONTENT and
# deprecated the old name. The status code itself is stable, so use the number and stay
# compatible with both versions.
HTTP_422_UNPROCESSABLE: int = 422


class DanahError(Exception):
    """Base class for every error DANAH raises deliberately."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        # `detail` is for logs and never leaves the process.
        self.detail: dict[str, Any] = detail or {}
        super().__init__(self.message)


class AuthError(DanahError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "auth_error"
    message = "Authentication failed."


class PermissionDeniedError(DanahError):
    """Authenticated but not allowed (role or clearance)."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class NotFoundError(DanahError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested resource was not found."


class InvalidRequestError(DanahError):
    status_code = HTTP_422_UNPROCESSABLE
    code = "validation_error"
    message = "The request was not valid."


class ConflictError(DanahError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The request conflicts with the current state."


class RateLimitError(DanahError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests."

    def __init__(self, message: str | None = None, *, retry_after: int = 60) -> None:
        super().__init__(message, detail={"retry_after": retry_after})
        self.retry_after = retry_after


class LLMGatewayError(DanahError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "llm_gateway_error"
    message = "The language model provider could not be reached."


class LLMNotConfiguredError(LLMGatewayError):
    """No provider credentials — PENDING-CREDENTIALS mode. Deliberately a 503."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "llm_not_configured"
    message = (
        "No language model provider is configured. Set ANTHROPIC_API_KEY (or OPENAI_API_KEY) "
        "and an embedding key in .env, then restart. See FIRST_RUN.md."
    )


class IngestionError(DanahError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "ingestion_error"
    message = "A data source could not be ingested."


class RetrievalError(DanahError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "retrieval_error"
    message = "Retrieval over the knowledge base failed."


class OrchestrationError(DanahError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "orchestration_error"
    message = "The agent pipeline failed."


class ApprovalError(DanahError):
    status_code = status.HTTP_409_CONFLICT
    code = "approval_error"
    message = "The approval could not be recorded."


class TokenBudgetExceededError(OrchestrationError):
    code = "token_budget_exceeded"
    message = "The pipeline exceeded its configured token budget."


def _error_body(code: str, message: str, request_id: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the global handlers. Every response shape here matches §3.6."""

    @app.exception_handler(DanahError)
    async def _handle_danah_error(_: Request, exc: DanahError) -> JSONResponse:
        request_id = get_request_id()
        # 5xx are our fault: log loudly with detail. 4xx are the caller's: log at info.
        logger = log.error if exc.status_code >= 500 else log.info
        logger(
            "request_failed",
            error_code=exc.code,
            status_code=exc.status_code,
            message=exc.message,
            **exc.detail,
        )
        headers: dict[str, str] = {}
        if isinstance(exc, RateLimitError):
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message, request_id),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = get_request_id()
        # Field names/locations are safe to return; submitted values are not (they may
        # carry credentials or OFFICIAL content), so only `loc` and `msg` are echoed.
        fields = [
            {"field": ".".join(str(p) for p in err.get("loc", ())), "message": err.get("msg", "")}
            for err in exc.errors()
        ]
        log.info("request_validation_failed", fields=fields)
        body = _error_body(
            "validation_error",
            "The request was not valid.",
            request_id,
        )
        body["error"]["fields"] = fields  # type: ignore[assignment]
        return JSONResponse(status_code=HTTP_422_UNPROCESSABLE, content=body)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        request_id = get_request_id()
        code = {
            401: "auth_error",
            403: "permission_denied",
            404: "not_found",
            405: "method_not_allowed",
        }.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(code, str(exc.detail), request_id),
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        request_id = get_request_id()
        # exc_info goes to the log; the client gets nothing but a request id to quote.
        log.exception("unhandled_exception", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body(
                "internal_error",
                "An unexpected error occurred. Quote the request_id when reporting this.",
                request_id,
            ),
        )
