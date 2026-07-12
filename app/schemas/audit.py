"""Audit trail + hash-chain verification schemas (§7.7 #23)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.enums import ActorType
from app.schemas.common import DanahModel


class AuditEntryOut(DanahModel):
    id: int
    ts: datetime
    actor_id: uuid.UUID | None = None
    actor_type: ActorType
    action: str
    subject_type: str | None = None
    subject_id: str | None = None
    ip: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    entry_hash: str


class AuditVerifyResponse(DanahModel):
    """`valid: false` pinpoints the first entry whose recomputed hash disagrees with the stored
    one — i.e. the row that was tampered with, or the first row after a deletion."""

    valid: bool
    entries_checked: int
    broken_at_id: int | None = Field(
        default=None, description="audit_log.id of the first entry that fails verification"
    )
    broken_at_index: int | None = Field(
        default=None, description="0-based position of that entry in the walked chain"
    )
    reason: str | None = None
    first_id: int | None = None
    last_id: int | None = None
    verified_at: datetime
