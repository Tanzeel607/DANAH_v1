"""Phase 0 gate: the app boots, healthz reports dependency status, errors are structured."""

from __future__ import annotations

from httpx import AsyncClient


async def test_healthz_returns_200_and_dependency_status(client: AsyncClient) -> None:
    resp = await client.get("/api/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"
    assert body["environment"] == "development"
    # PENDING-CREDENTIALS: the service is healthy without provider keys.
    assert body["llm_configured"] is False
    assert "version" in body


async def test_request_id_is_echoed(client: AsyncClient) -> None:
    resp = await client.get("/api/healthz", headers={"X-Request-ID": "test-request-id-123"})

    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "test-request-id-123"


async def test_request_id_is_generated_when_absent(client: AsyncClient) -> None:
    resp = await client.get("/api/healthz")

    assert resp.headers.get("X-Request-ID")


async def test_unknown_route_returns_structured_error(client: AsyncClient) -> None:
    """Errors never leak a stack trace; they always carry a quotable request_id (§3.6)."""
    resp = await client.get("/api/does-not-exist")

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"]
    assert "Traceback" not in resp.text


async def test_metrics_endpoint_exposed(client: AsyncClient) -> None:
    resp = await client.get("/metrics")

    assert resp.status_code == 200
    assert "python_info" in resp.text or "http_request" in resp.text


async def test_openapi_schema_is_served(client: AsyncClient) -> None:
    resp = await client.get("/openapi.json")

    assert resp.status_code == 200
    schema = resp.json()
    assert schema["info"]["title"].startswith("DANAH")
    assert "/api/healthz" in schema["paths"]
