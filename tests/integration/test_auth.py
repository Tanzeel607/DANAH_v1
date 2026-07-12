"""Auth API integration tests (§7.7 #1-3) and the security properties behind them."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Classification, Role
from app.models import RefreshToken, User

PASSWORD = "correct-horse-battery-staple"


class TestLogin:
    async def test_login_returns_a_token_pair(self, client: AsyncClient, user_factory: Any) -> None:
        user = await user_factory(role=Role.ANALYST, password=PASSWORD)

        resp = await client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0

    async def test_wrong_password_is_rejected(self, client: AsyncClient, user_factory: Any) -> None:
        user = await user_factory(password=PASSWORD)

        resp = await client.post(
            "/api/auth/login", json={"email": user.email, "password": "wrong-password"}
        )

        assert resp.status_code == 401

    async def test_unknown_email_and_wrong_password_are_indistinguishable(
        self, client: AsyncClient, user_factory: Any
    ) -> None:
        """User enumeration defence: the response must not reveal whether the account exists."""
        user = await user_factory(password=PASSWORD)

        unknown = await client.post(
            "/api/auth/login", json={"email": "nobody@ministry.gov", "password": PASSWORD}
        )
        wrong = await client.post(
            "/api/auth/login", json={"email": user.email, "password": "wrong-password"}
        )

        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]

    async def test_inactive_user_cannot_log_in(
        self, client: AsyncClient, user_factory: Any
    ) -> None:
        user = await user_factory(password=PASSWORD, is_active=False)

        resp = await client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )

        assert resp.status_code == 401

    async def test_password_is_never_stored_in_plaintext(
        self, db: AsyncSession, user_factory: Any
    ) -> None:
        user = await user_factory(password=PASSWORD)

        stored = await db.get(User, user.id)

        assert stored is not None
        assert PASSWORD not in stored.password_hash
        assert stored.password_hash.startswith("$argon2")


class TestMe:
    async def test_returns_profile_with_derived_clearance(
        self, client: AsyncClient, auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.ANALYST)

        resp = await client.get("/api/auth/me", headers=headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["role"] == Role.ANALYST.value
        assert body["clearance"] == Classification.OFFICIAL.value

    async def test_clearance_matches_the_role_table(
        self, client: AsyncClient, auth_headers: Any
    ) -> None:
        """Architecture §8: viewer→INTERNAL, analyst→OFFICIAL, executive/admin→OFFICIAL_SENSITIVE."""
        expected = {
            Role.VIEWER: Classification.INTERNAL,
            Role.ANALYST: Classification.OFFICIAL,
            Role.EXECUTIVE: Classification.OFFICIAL_SENSITIVE,
            Role.ADMIN: Classification.OFFICIAL_SENSITIVE,
        }

        for role, clearance in expected.items():
            headers = await auth_headers(role)
            resp = await client.get("/api/auth/me", headers=headers)

            assert resp.status_code == 200
            assert resp.json()["clearance"] == clearance.value

    async def test_requires_a_token(self, client: AsyncClient) -> None:
        resp = await client.get("/api/auth/me")

        assert resp.status_code == 401

    async def test_garbage_token_is_rejected(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
        )

        assert resp.status_code == 401


class TestRefreshRotation:
    async def test_refresh_returns_a_new_pair(self, client: AsyncClient, user_factory: Any) -> None:
        user = await user_factory(password=PASSWORD)
        login = await client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        original = login.json()["refresh_token"]

        resp = await client.post("/api/auth/refresh", json={"refresh_token": original})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["refresh_token"] != original, "refresh tokens must rotate"
        assert body["access_token"]

    async def test_refresh_tokens_are_stored_hashed(
        self, client: AsyncClient, db: AsyncSession, user_factory: Any
    ) -> None:
        """A database leak must not yield usable refresh tokens."""
        user = await user_factory(password=PASSWORD)
        login = await client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        token = login.json()["refresh_token"]

        rows = (await db.scalars(select(RefreshToken))).all()

        assert rows
        assert all(row.token_hash != token for row in rows)
        assert all(len(row.token_hash) == 64 for row in rows)  # sha256 hex

    async def test_replaying_a_rotated_token_is_rejected(
        self, client: AsyncClient, user_factory: Any
    ) -> None:
        user = await user_factory(password=PASSWORD)
        login = await client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        first = login.json()["refresh_token"]

        await client.post("/api/auth/refresh", json={"refresh_token": first})
        replay = await client.post("/api/auth/refresh", json={"refresh_token": first})

        assert replay.status_code == 401

    async def test_replay_revokes_the_whole_token_family(
        self, client: AsyncClient, user_factory: Any
    ) -> None:
        """Reuse of a revoked token is the signature of a stolen token being replayed.

        The safe response is to invalidate every refresh token the user holds — the legitimate
        user re-authenticates, and the thief's stolen token dies with the family.
        """
        user = await user_factory(password=PASSWORD)
        login = await client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        stolen = login.json()["refresh_token"]

        rotated = await client.post("/api/auth/refresh", json={"refresh_token": stolen})
        current = rotated.json()["refresh_token"]

        # The thief replays the old token — detected.
        await client.post("/api/auth/refresh", json={"refresh_token": stolen})

        # The legitimate user's current token must now also be dead.
        resp = await client.post("/api/auth/refresh", json={"refresh_token": current})
        assert resp.status_code == 401

    async def test_access_token_cannot_be_used_as_a_refresh_token(
        self, client: AsyncClient, user_factory: Any
    ) -> None:
        user = await user_factory(password=PASSWORD)
        login = await client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        access = login.json()["access_token"]

        resp = await client.post("/api/auth/refresh", json={"refresh_token": access})

        assert resp.status_code == 401

    async def test_refresh_token_cannot_be_used_as_a_bearer_credential(
        self, client: AsyncClient, user_factory: Any
    ) -> None:
        user = await user_factory(password=PASSWORD)
        login = await client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        refresh = login.json()["refresh_token"]

        resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {refresh}"})

        assert resp.status_code == 401


class TestRoleEnforcement:
    async def test_viewer_cannot_upload_documents(
        self, client: AsyncClient, auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.VIEWER)

        resp = await client.post(
            "/api/knowledge/documents",
            headers=headers,
            files={"file": ("test.txt", b"content", "text/plain")},
        )

        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "permission_denied"

    async def test_analyst_can_upload_documents(
        self, client: AsyncClient, auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.ANALYST)

        resp = await client.post(
            "/api/knowledge/documents",
            headers=headers,
            files={"file": ("test.txt", b"Ministry strategy content.", "text/plain")},
        )

        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "pending"
