"""Phase 4 — rate limiting (master prompt §10: "rate limits return 429 with Retry-After").

These tests need a real Redis (the sliding window is a Redis sorted set). They are skipped, not
faked, when Redis is unavailable: a fake in-memory limiter would test a different algorithm than
the one that ships, and the boundary behaviour is the whole point.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import get_settings
from app.enums import Role
from app.security.rate_limit import RateLimiter, reset_limiter


async def redis_available() -> bool:
    try:
        client = Redis.from_url(get_settings().redis_url, socket_connect_timeout=1)
        await client.ping()
        await client.aclose()
    except (RedisError, OSError):
        return False
    return True


@pytest.fixture(autouse=True)
async def _clean_limiter() -> Any:
    """Each test starts with an empty window, or they would poison each other."""
    reset_limiter()
    if await redis_available():
        client = Redis.from_url(get_settings().redis_url)
        keys = [k async for k in client.scan_iter("ratelimit:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()
    yield
    reset_limiter()


class TestRateLimiter:
    async def test_allows_up_to_the_limit_then_blocks(self) -> None:
        if not await redis_available():
            pytest.skip("Redis is not available; the sliding window needs a real Redis")

        limiter = RateLimiter(get_settings().redis_url)

        for i in range(5):
            allowed, _ = await limiter.check(scope="test", identity="ip-1", limit=5)
            assert allowed is True, f"request {i + 1} of 5 should be allowed"

        allowed, retry_after = await limiter.check(scope="test", identity="ip-1", limit=5)

        assert allowed is False
        assert retry_after > 0
        await limiter.aclose()

    async def test_windows_are_isolated_per_identity(self) -> None:
        if not await redis_available():
            pytest.skip("Redis is not available")

        limiter = RateLimiter(get_settings().redis_url)

        for _ in range(5):
            await limiter.check(scope="test", identity="ip-A", limit=5)

        # A different caller must be unaffected by the first one's burst.
        allowed, _ = await limiter.check(scope="test", identity="ip-B", limit=5)

        assert allowed is True
        await limiter.aclose()

    async def test_scopes_are_isolated(self) -> None:
        if not await redis_available():
            pytest.skip("Redis is not available")

        limiter = RateLimiter(get_settings().redis_url)

        for _ in range(5):
            await limiter.check(scope="login", identity="same", limit=5)

        allowed, _ = await limiter.check(scope="chat", identity="same", limit=5)

        assert allowed is True, "exhausting the login window must not block chat"
        await limiter.aclose()

    async def test_fails_open_when_redis_is_unreachable(self) -> None:
        """A rate-limiter outage must not become an authentication outage."""
        limiter = RateLimiter("redis://127.0.0.1:6399/0")  # nothing listens here

        allowed, retry_after = await limiter.check(scope="test", identity="x", limit=1)

        assert allowed is True
        assert retry_after == 0
        await limiter.aclose()


class TestLoginRateLimit:
    async def test_login_burst_returns_429_with_retry_after(
        self, client: AsyncClient, user_factory: Any
    ) -> None:
        """§10 Phase 4: rate limits return 429 with Retry-After."""
        if not await redis_available():
            pytest.skip("Redis is not available")

        limit = get_settings().rate_limit_login_per_minute
        statuses: list[int] = []

        for _ in range(limit + 3):
            resp = await client.post(
                "/api/auth/login",
                json={"email": "attacker@example.test", "password": "guess"},
            )
            statuses.append(resp.status_code)
            if resp.status_code == 429:
                assert resp.headers.get("Retry-After") is not None
                assert int(resp.headers["Retry-After"]) > 0
                assert resp.json()["error"]["code"] == "rate_limited"
                break

        assert 429 in statuses, (
            f"a burst of {limit + 3} logins must be rate limited (got {statuses})"
        )

    async def test_credential_stuffing_is_limited_by_ip_not_by_email(
        self, client: AsyncClient
    ) -> None:
        """A different email per attempt is exactly what credential stuffing looks like."""
        if not await redis_available():
            pytest.skip("Redis is not available")

        limit = get_settings().rate_limit_login_per_minute
        blocked = False

        for i in range(limit + 3):
            resp = await client.post(
                "/api/auth/login",
                json={"email": f"victim{i}@ministry.gov", "password": "guess"},
            )
            if resp.status_code == 429:
                blocked = True
                break

        assert blocked, "varying the email must not reset the window"


class TestChatRateLimit:
    async def test_chat_burst_is_limited_per_user(
        self, client: AsyncClient, auth_headers: Any
    ) -> None:
        if not await redis_available():
            pytest.skip("Redis is not available")

        headers = await auth_headers(Role.ANALYST)
        limit = get_settings().rate_limit_chat_per_minute
        blocked = False

        for _ in range(limit + 3):
            resp = await client.post("/api/agent/chat", headers=headers, json={"message": "hello"})
            if resp.status_code == 429:
                blocked = True
                assert resp.headers.get("Retry-After") is not None
                break

        assert blocked, f"a burst of {limit + 3} chat requests must be rate limited"
