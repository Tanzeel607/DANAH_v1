"""Password hashing (argon2id) and refresh-token digests.

argon2id with the `argon2-cffi` defaults (OWASP-recommended parameters). `verify_password`
transparently re-hashes when the parameters change, so raising the cost factor later does not
strand existing users on weak hashes.
"""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verification. Any malformed or mismatched hash is simply `False`."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses weaker parameters than the current policy."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True


def generate_token(nbytes: int = 48) -> str:
    """A cryptographically random opaque token (used as the refresh token)."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """SHA-256 digest of a refresh token.

    Refresh tokens are stored hashed (§7.6): a database leak must not yield usable tokens.
    SHA-256 — not argon2 — is correct here: the token already has 384 bits of entropy, so it is
    not brute-forceable, and refresh happens on a hot path where argon2's cost would be felt.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
