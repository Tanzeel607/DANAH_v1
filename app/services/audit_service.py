"""Hash-chained, append-only audit log.

`entry_hash = sha256(prev_hash + canonical_json(entry))`. Each entry commits to its predecessor,
so altering *any* historical row invalidates every hash after it. Deleting a row breaks the chain
at the gap. `verify_chain()` re-walks the chain and names the first entry that fails — which is
the tampered one, or the one after a deletion.

Two properties make this real rather than decorative:

  * **The database refuses to help.** A trigger (migration 0001) rejects UPDATE, DELETE and
    TRUNCATE on `audit_log`. The application literally cannot rewrite history; only a superuser
    disabling the trigger can — and that is exactly the attack `verify_chain` detects.

  * **Canonical JSON.** Hashing must be byte-identical on every re-walk, so the serialisation is
    pinned: sorted keys, no insignificant whitespace, UTF-8, no ASCII escaping. A change to this
    function invalidates every existing chain, so it is deliberately boring and must stay that way.

Writes are serialised with a table-level lock. Two concurrent appends reading the same `prev_hash`
would fork the chain and make verification fail for a reason no attacker caused.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ActorType
from app.models import AuditLog

log = structlog.get_logger(__name__)

# The chain's anchor. The first entry's prev_hash is this constant, so an attacker cannot
# truncate the log to zero rows and start a fresh, self-consistent chain without it being obvious.
GENESIS_HASH = "0" * 64


def canonical_json(payload: dict[str, Any]) -> str:
    """Byte-stable JSON. Changing this invalidates every chain ever written."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def entry_payload(
    *,
    ts: datetime,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    action: str,
    subject_type: str | None,
    subject_id: str | None,
    ip: str | None,
    detail: dict[str, Any],
) -> dict[str, Any]:
    """The exact fields that are hashed.

    `id` is excluded on purpose: it is assigned by the sequence *after* the hash is computed, and
    including it would make the hash depend on insertion order in a way that a legitimate restore
    could not reproduce.
    """
    return {
        "ts": ts.astimezone(UTC).isoformat(),
        "actor_id": str(actor_id) if actor_id else None,
        "actor_type": actor_type.value,
        "action": action,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "ip": ip,
        "detail": detail,
    }


def compute_entry_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical_json(payload)).encode("utf-8")).hexdigest()


async def record_audit(
    session: AsyncSession,
    *,
    action: str,
    actor_type: ActorType,
    actor_id: uuid.UUID | None = None,
    subject_type: str | None = None,
    subject_id: str | uuid.UUID | None = None,
    ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one entry. Participates in the caller's transaction.

    Sharing the caller's transaction is deliberate: if the action being audited rolls back, its
    audit entry must roll back too, or the log would assert that something happened which did not.
    (The cost ledger takes the opposite decision, for the opposite reason — see `usage_tracker`.)
    """
    # Serialise appends. Without this, two concurrent writers can read the same tail and compute
    # two entries with the same prev_hash, forking the chain.
    await session.execute(text("LOCK TABLE audit_log IN EXCLUSIVE MODE"))

    prev_hash = await _tail_hash(session)
    ts = datetime.now(UTC)

    payload = entry_payload(
        ts=ts,
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        subject_type=subject_type,
        subject_id=str(subject_id) if subject_id else None,
        ip=ip,
        detail=detail or {},
    )
    entry_hash = compute_entry_hash(prev_hash, payload)

    entry = AuditLog(
        ts=ts,
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        subject_type=subject_type,
        subject_id=str(subject_id) if subject_id else None,
        ip=ip,
        detail=payload["detail"],
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    session.add(entry)
    await session.flush()

    log.info(
        "audit",
        action=action,
        actor_type=actor_type.value,
        actor_id=str(actor_id) if actor_id else None,
        subject_type=subject_type,
        subject_id=str(subject_id) if subject_id else None,
    )
    return entry


async def _tail_hash(session: AsyncSession) -> str:
    last = await session.scalar(select(AuditLog.entry_hash).order_by(AuditLog.id.desc()).limit(1))
    return str(last) if last else GENESIS_HASH


@dataclass(slots=True)
class VerificationResult:
    valid: bool
    entries_checked: int
    broken_at_id: int | None = None
    broken_at_index: int | None = None
    reason: str | None = None
    first_id: int | None = None
    last_id: int | None = None


async def verify_chain(session: AsyncSession, *, limit: int | None = None) -> VerificationResult:
    """Re-walk the chain from the genesis anchor and report the first entry that fails.

    Three distinct failures are detected:
      * a row's contents were altered  → its recomputed `entry_hash` no longer matches
      * a row was deleted              → the next row's `prev_hash` does not match its predecessor
      * a row was re-hashed to cover an edit → the *following* row's prev_hash link breaks

    The tamperer must rewrite every subsequent row to stay consistent — and a `prev_hash` of the
    very first row that is not GENESIS reveals a truncation.
    """
    stmt = select(AuditLog).order_by(AuditLog.id)
    if limit is not None:
        stmt = stmt.limit(limit)

    entries = list((await session.scalars(stmt)).all())
    if not entries:
        return VerificationResult(valid=True, entries_checked=0)

    expected_prev = GENESIS_HASH

    for index, entry in enumerate(entries):
        if entry.prev_hash != expected_prev:
            return VerificationResult(
                valid=False,
                entries_checked=index,
                broken_at_id=entry.id,
                broken_at_index=index,
                reason=(
                    "prev_hash does not match the previous entry's hash — an entry was deleted, "
                    "reordered, or inserted."
                ),
                first_id=entries[0].id,
                last_id=entries[-1].id,
            )

        payload = entry_payload(
            ts=entry.ts,
            actor_id=entry.actor_id,
            actor_type=entry.actor_type,
            action=entry.action,
            subject_type=entry.subject_type,
            subject_id=entry.subject_id,
            ip=entry.ip,
            detail=entry.detail,
        )
        recomputed = compute_entry_hash(entry.prev_hash, payload)

        if recomputed != entry.entry_hash:
            return VerificationResult(
                valid=False,
                entries_checked=index,
                broken_at_id=entry.id,
                broken_at_index=index,
                reason="entry_hash does not match the entry's contents — this row was modified.",
                first_id=entries[0].id,
                last_id=entries[-1].id,
            )

        expected_prev = entry.entry_hash

    return VerificationResult(
        valid=True,
        entries_checked=len(entries),
        first_id=entries[0].id,
        last_id=entries[-1].id,
    )
