"""Authentication: password login, token issue, and refresh rotation with reuse detection.

Pure service functions — no FastAPI types cross this boundary, so the same logic is callable
from the API, the seed script and the ARQ worker.

The refresh model is *rotating and single-use* (§7.6): every refresh mints a new pair and spends
the presented one. That is what makes replay of a stolen token detectable at all — see
`rotate_refresh_token`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from functools import lru_cache

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.exceptions import AuthError
from app.models import RefreshToken, User
from app.security import jwt as jwt_tokens
from app.security import passwords

log = structlog.get_logger(__name__)

# One message for every credential failure. Distinguishing "no such account" from "wrong
# password" hands an attacker a free account-enumeration oracle.
_INVALID_CREDENTIALS = "Invalid email or password."
_INVALID_REFRESH = "Invalid or expired refresh token."


@lru_cache(maxsize=1)
def _decoy_hash() -> str:
    """An argon2 hash of a random string, verified against when no user row exists.

    Built lazily and memoised: it must carry the *current* cost parameters, because its whole
    purpose is to cost exactly as much to verify as a real stored hash.
    """
    return passwords.hash_password(passwords.generate_token())


async def _verify(password: str, password_hash: str) -> bool:
    """argon2id is deliberately expensive (tens of milliseconds of CPU by design).

    Run it in a worker thread: on the single-threaded event loop a burst of logins would
    otherwise stall every unrelated request behind the hashes.
    """
    return await asyncio.to_thread(passwords.verify_password, password, password_hash)


def _burn_decoy(password: str) -> None:
    """Pay a real verification's CPU cost against the decoy. Called *inside* the worker thread.

    `_decoy_hash()` is resolved here rather than at the call site because its first invocation
    performs an argon2 *hash*, which is exactly as expensive as the verification it is standing
    in for — evaluating it as an argument would run that hash on the event loop.
    """
    passwords.verify_password(password, _decoy_hash())


async def authenticate(
    session: AsyncSession,
    settings: Settings,
    *,
    email: str,
    password: str,
    ip: str | None,
) -> User:
    """Verify credentials and return the user. Raises `AuthError` on any failure."""
    normalised = email.strip().lower()
    user = (
        await session.scalars(select(User).where(func.lower(User.email) == normalised))
    ).one_or_none()

    if user is None:
        # Timing oracle: returning here immediately would make an unknown address answer in
        # microseconds while a real one pays for an argon2 verification — a stopwatch would
        # then enumerate which addresses hold accounts. Burning the same CPU against a decoy
        # hash keeps the two paths indistinguishable from the outside.
        await asyncio.to_thread(_burn_decoy, password)
        log.info("login_failed", reason="unknown_email", ip=ip)
        raise AuthError(_INVALID_CREDENTIALS)

    if not await _verify(password, user.password_hash):
        log.info("login_failed", reason="bad_password", user_id=str(user.id), ip=ip)
        raise AuthError(_INVALID_CREDENTIALS)

    # Checked only after the password: a caller who cannot authenticate must not be able to
    # learn that an address exists but is disabled.
    if not user.is_active:
        log.info("login_failed", reason="inactive_account", user_id=str(user.id), ip=ip)
        raise AuthError("This account has been deactivated. Contact an administrator.")

    rehashed = False
    if passwords.needs_rehash(user.password_hash):
        # The cost parameters were raised since this hash was written. Login is the only moment
        # the plaintext is in hand, so it is the only chance to upgrade the stored hash.
        user.password_hash = await asyncio.to_thread(passwords.hash_password, password)
        rehashed = True

    user.last_login_at = datetime.now(UTC)
    await session.flush()

    log.info(
        "login_succeeded",
        user_id=str(user.id),
        role=user.role.value,
        ip=ip,
        password_rehashed=rehashed,
    )
    return user


async def issue_token_pair(
    session: AsyncSession,
    settings: Settings,
    user: User,
) -> tuple[str, str, int]:
    """Mint a stateless access JWT plus an opaque, persisted refresh token.

    Returns `(access_token, refresh_token, expires_in_seconds)`. The refresh token is returned
    to the caller in plaintext exactly once; only its SHA-256 digest is stored, so a database
    leak yields nothing usable.
    """
    access_token, expires_in = jwt_tokens.create_access_token(
        settings,
        user_id=user.id,
        role=user.role.value,
        email=user.email,
    )

    refresh_token = passwords.generate_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=passwords.hash_token(refresh_token),
            expires_at=jwt_tokens.refresh_token_expiry(settings),
        )
    )
    await session.flush()

    log.info("token_pair_issued", user_id=str(user.id), expires_in=expires_in)
    return access_token, refresh_token, expires_in


async def rotate_refresh_token(
    session: AsyncSession,
    settings: Settings,
    *,
    refresh_token: str,
) -> tuple[User, str, str, int]:
    """Spend a refresh token and issue a fresh pair.

    Returns `(user, access_token, refresh_token, expires_in_seconds)`.
    """
    row = (
        await session.scalars(
            select(RefreshToken).where(
                RefreshToken.token_hash == passwords.hash_token(refresh_token)
            )
        )
    ).one_or_none()

    if row is None:
        log.info("refresh_rejected", reason="unknown_token")
        raise AuthError(_INVALID_REFRESH)

    if row.revoked_at is not None:
        # REUSE DETECTION. Rotation makes each refresh token single-use, so a token that was
        # already spent being presented again means two parties hold it: the legitimate client
        # and whoever copied it. We cannot tell which one is on this connection, so we assume
        # theft and revoke the whole family — every outstanding token for the user, including
        # the one the thief rotated into. Both sides are forced back to /login, which the
        # attacker cannot pass without the password.
        revoked = await revoke_all_for_user(session, user_id=row.user_id)
        # Commit — not merely flush — *before* raising. This is the one write in the codebase
        # that has to outlive its own request failing: `deps.get_db` rolls the session back on
        # any exception, so a flushed-only revocation would be thrown away the instant the
        # `AuthError` below propagates, leaving the stolen family live for its full 14 days
        # while the log line claimed it had been killed. The session holds nothing else at this
        # point (the request has only read), so there is no unrelated work to commit early.
        await session.commit()
        log.warning(
            "refresh_token_reuse_detected",
            user_id=str(row.user_id),
            revoked_count=revoked,
        )
        raise AuthError(_INVALID_REFRESH)

    now = datetime.now(UTC)
    if row.expires_at <= now:
        log.info("refresh_rejected", reason="expired", user_id=str(row.user_id))
        raise AuthError(_INVALID_REFRESH)

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        # The account was deleted or disabled after the token was minted; a long-lived refresh
        # token must not outlive its owner's access.
        log.info("refresh_rejected", reason="inactive_account", user_id=str(row.user_id))
        raise AuthError(_INVALID_REFRESH)

    row.revoked_at = now
    access_token, new_refresh_token, expires_in = await issue_token_pair(session, settings, user)

    log.info("refresh_rotated", user_id=str(user.id))
    return user, access_token, new_refresh_token, expires_in


async def revoke_all_for_user(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """Revoke every outstanding refresh token for a user. Returns how many were revoked.

    Loaded and revoked through the ORM rather than as a bulk UPDATE: the caller usually holds a
    `RefreshToken` from this same family in the identity map, and a bulk statement would leave
    that instance stale.
    """
    rows = (
        await session.scalars(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
        )
    ).all()

    now = datetime.now(UTC)
    for row in rows:
        row.revoked_at = now
    await session.flush()

    log.info("refresh_tokens_revoked", user_id=str(user_id), count=len(rows))
    return len(rows)
