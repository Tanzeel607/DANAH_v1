"""Notification schemas (§7.8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.enums import NotificationKind
from app.schemas.common import DanahModel


class NotificationOut(DanahModel):
    id: uuid.UUID
    kind: NotificationKind
    title: str
    body: str
    subject_ref: dict[str, Any] = Field(default_factory=dict)
    read_at: datetime | None = None
    created_at: datetime

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class MarkReadRequest(DanahModel):
    ids: list[uuid.UUID] = Field(
        default_factory=list, description="Empty list marks every unread notification as read"
    )


class MarkReadResponse(DanahModel):
    marked: int
