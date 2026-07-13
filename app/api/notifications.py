"""Notifications API (§7.8). Mounted at /api/notifications.

A notification reaches a user if it names them personally or names their role — an approval that
notifies nobody sits in the queue forever.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.models import Notification, User
from app.schemas.notifications import MarkReadRequest, MarkReadResponse, NotificationOut
from app.services.notification_service import list_notifications, mark_read

router = APIRouter(tags=["notifications"])


def _notification_out(notification: Notification) -> NotificationOut:
    return NotificationOut(
        id=notification.id,
        kind=notification.kind,
        title=notification.title,
        body=notification.body,
        subject_ref=notification.subject_ref,
        read_at=notification.read_at,
        created_at=notification.created_at,
    )


@router.get(
    "",
    response_model=list[NotificationOut],
    summary="Your notifications, newest first",
    description="Everything addressed to you personally, plus everything addressed to your role.",
)
async def list_all(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[NotificationOut]:
    notifications = await list_notifications(
        db,
        user=user,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )
    return [_notification_out(n) for n in notifications]


@router.post(
    "/read",
    response_model=MarkReadResponse,
    summary="Mark notifications read (empty `ids` marks them all)",
    description=(
        "The 'is it yours' predicate is part of the UPDATE, not a check performed before it, so "
        "another user's notification cannot be marked read by guessing its id."
    ),
)
async def mark_notifications_read(
    payload: MarkReadRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MarkReadResponse:
    marked = await mark_read(db, user=user, ids=payload.ids)
    return MarkReadResponse(marked=marked)
