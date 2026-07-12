"""Approval queue schemas (§7.7 #19–20)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from app.enums import AgentName, ApprovalStatus, ApprovalSubject, Role
from app.schemas.common import DanahModel


class ApprovalOut(DanahModel):
    id: uuid.UUID
    subject_type: ApprovalSubject
    subject_id: uuid.UUID
    requested_by_agent: AgentName
    assigned_role: Role
    status: ApprovalStatus
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    comment: str | None = None
    created_at: datetime
    # Denormalised so the queue renders without an N+1 fetch per row.
    subject_title: str = ""
    subject_summary: str = ""
    subject_confidence: float | None = None
    subject_severity: int | None = None


class ApprovalDecisionRequest(DanahModel):
    decision: Literal["approved", "rejected", "changes_requested"]
    comment: Annotated[str, Field(max_length=4000)] = ""


class ApprovalDecisionResponse(DanahModel):
    id: uuid.UUID
    status: ApprovalStatus
    subject_type: ApprovalSubject
    subject_id: uuid.UUID
    subject_status: str = Field(description="Resulting publication status of the subject")
    decided_by: uuid.UUID
    decided_at: datetime
