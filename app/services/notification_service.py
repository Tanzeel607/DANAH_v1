"""Notifications (master prompt §7.8).

Every notification is written to the `notifications` table first and emailed second. The row is
the system of record; email is a courtesy copy. That order matters: SMTP is the least reliable
dependency DANAH has, and a mail server that is down must never cost an executive the knowledge
that a briefing is waiting for their decision.

Hence the hard rule in `send_email`: **it never raises**. With `SMTP_HOST` unset it runs in
log-only mode — it records what it *would* have sent and returns False — so a fresh clone with no
mail configuration still exercises the whole approval path end to end.

Notification content (a briefing title, an insight title) can be OFFICIAL-SENSITIVE, so it is
written to the database and put in the mail body, but never to the logs. The logs carry kinds,
ids, counts and sizes.
"""

from __future__ import annotations

import uuid
from email.message import EmailMessage
from typing import Any, Final

import aiosmtplib
import structlog
from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.enums import ApprovalSubject, NotificationKind, Role
from app.exceptions import InvalidRequestError
from app.models import Notification, User

log = structlog.get_logger(__name__)

# Subject-line prefix, so a ministry mailbox rule can file DANAH mail without parsing the body.
SUBJECT_PREFIX: Final[str] = "[DANAH]"


async def notify(
    session: AsyncSession,
    *,
    kind: NotificationKind,
    title: str,
    body: str,
    subject_ref: dict[str, Any],
    user_id: uuid.UUID | None = None,
    role: Role | None = None,
) -> Notification:
    """Write one in-app notification, addressed to a person OR to a role — never both.

    The table has a CHECK constraint saying the same thing. It is validated here as well so the
    caller gets a 422 naming the mistake instead of an IntegrityError surfacing as a 500.
    """
    if (user_id is None) == (role is None):
        raise InvalidRequestError(
            "A notification must be addressed to exactly one of user_id or role.",
            detail={
                "user_id": str(user_id) if user_id else None,
                "role": role.value if role else None,
            },
        )

    notification = Notification(
        id=uuid.uuid4(),
        user_id=user_id,
        role=role,
        kind=kind,
        title=title,
        body=body,
        subject_ref=subject_ref,
    )
    session.add(notification)
    await session.flush()

    log.info(
        "notification_created",
        notification_id=str(notification.id),
        kind=kind.value,
        user_id=str(user_id) if user_id else None,
        role=role.value if role else None,
        subject_ref=subject_ref,
        # Title and body are omitted: they quote the subject, which may be OFFICIAL-SENSITIVE.
    )
    return notification


async def notify_approval_pending(
    session: AsyncSession,
    *,
    subject_type: ApprovalSubject,
    subject_id: uuid.UUID,
    title: str,
    settings: Settings | None = None,
) -> None:
    """An agent produced something; a human must now decide. Addressed to the executive role."""
    body = (
        f"A {subject_type.value} is waiting for your decision.\n\n"
        f"{title}\n\n"
        "Open the approvals queue to approve, reject, or request changes. Nothing is visible to "
        "readers until you approve it."
    )
    await notify(
        session,
        kind=NotificationKind.APPROVAL_PENDING,
        title=f"Approval required: {title}",
        body=body,
        subject_ref={"type": subject_type.value, "id": str(subject_id)},
        role=Role.EXECUTIVE,
    )
    await _email_role(
        session,
        role=Role.EXECUTIVE,
        subject=f"{SUBJECT_PREFIX} Approval required: {title}",
        body=body,
        settings=settings,
    )


async def notify_briefing_published(
    session: AsyncSession,
    *,
    briefing_id: uuid.UUID,
    title: str,
    settings: Settings | None = None,
) -> None:
    """A human approved a briefing and it is now readable."""
    body = (
        f"The executive briefing '{title}' has been approved and published.\n\n"
        "It is now visible in DANAH to everyone with the clearance to read it."
    )
    await notify(
        session,
        kind=NotificationKind.BRIEFING_PUBLISHED,
        title=f"Briefing published: {title}",
        body=body,
        subject_ref={"type": "briefing", "id": str(briefing_id)},
        role=Role.EXECUTIVE,
    )
    await _email_role(
        session,
        role=Role.EXECUTIVE,
        subject=f"{SUBJECT_PREFIX} Briefing published: {title}",
        body=body,
        settings=settings,
    )


async def notify_cost_alert(
    session: AsyncSession,
    *,
    spent_usd: float,
    threshold_usd: float,
    settings: Settings | None = None,
) -> None:
    """Today's LLM spend crossed `DAILY_COST_ALERT_USD`. Addressed to admins, who own the budget."""
    title = f"LLM spend today is ${spent_usd:.2f} (alert threshold ${threshold_usd:.2f})"
    body = (
        f"DANAH has spent ${spent_usd:.2f} on language-model calls since midnight UTC, which is "
        f"at or above the configured alert threshold of ${threshold_usd:.2f}.\n\n"
        "Review the dashboard cost panel. Raise DAILY_COST_ALERT_USD if this is expected, or "
        "reduce PIPELINE_MAX_ITEMS_PER_RUN / PIPELINE_TOKEN_BUDGET if it is not."
    )
    await notify(
        session,
        kind=NotificationKind.COST_ALERT,
        title=title,
        body=body,
        subject_ref={
            "type": "cost",
            "spent_usd": round(spent_usd, 2),
            "threshold_usd": threshold_usd,
        },
        role=Role.ADMIN,
    )
    await _email_role(
        session,
        role=Role.ADMIN,
        subject=f"{SUBJECT_PREFIX} {title}",
        body=body,
        settings=settings,
    )


async def send_email(settings: Settings, *, to: str, subject: str, body: str) -> bool:
    """Send one plain-text email. Returns True only if a mail server accepted it.

    This function never raises (master prompt §7.8). A notification is already durable in the
    database by the time it is called, so a failure here degrades a courtesy copy — it must not
    fail the approval, the publication, or the pipeline run that triggered it.
    """
    if not settings.smtp_host.strip():
        # Log-only mode. Sizes, not text: a subject line carries the briefing's title.
        log.info(
            "email_not_sent_no_smtp_host",
            to=to,
            subject_chars=len(subject),
            body_chars=len(body),
            hint="Set SMTP_HOST to deliver notification email.",
        )
        return False

    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    username = settings.smtp_username.strip()
    password = settings.smtp_password.get_secret_value()

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            start_tls=settings.smtp_use_tls,
            username=username or None,
            password=password or None,
            timeout=settings.llm_timeout_seconds,
        )
    except Exception as exc:
        # Deliberately broad: aiosmtplib raises a dozen distinct errors, DNS and TLS raise more,
        # and §7.8 says none of them may reach the caller. The notification row is already safe.
        log.error(
            "email_send_failed",
            to=to,
            error_type=type(exc).__name__,
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
        )
        return False

    log.info("email_sent", to=to, subject_chars=len(subject), body_chars=len(body))
    return True


async def list_notifications(
    session: AsyncSession,
    *,
    user: User,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[Notification]:
    """Everything addressed to this user personally, plus everything addressed to their role."""
    stmt = (
        select(Notification)
        .where(_addressed_to(user))
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))

    return list((await session.scalars(stmt)).all())


async def count_unread(session: AsyncSession, *, user: User) -> int:
    """The badge number."""
    total = await session.scalar(
        select(func.count(Notification.id))
        .select_from(Notification)
        .where(_addressed_to(user), Notification.read_at.is_(None))
    )
    return int(total or 0)


async def mark_read(session: AsyncSession, *, user: User, ids: list[uuid.UUID]) -> int:
    """Mark the given notifications read; an empty list marks every unread one. Returns the count.

    The `addressed_to` predicate is part of the UPDATE, not a check before it, so a user cannot
    mark another user's notification read by guessing its id.
    """
    stmt = (
        update(Notification)
        .where(_addressed_to(user), Notification.read_at.is_(None))
        .values(read_at=func.now())
    )
    if ids:
        stmt = stmt.where(Notification.id.in_(ids))

    # RETURNING rather than `rowcount`: it is exact, it is typed, and it tells us precisely which
    # rows this user was actually allowed to mark.
    updated = (await session.scalars(stmt.returning(Notification.id))).all()
    marked = len(updated)

    log.info("notifications_marked_read", user_id=str(user.id), marked=marked, requested=len(ids))
    return marked


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _addressed_to(user: User) -> ColumnElement[bool]:
    """A notification reaches a user if it names them, or if it names their role."""
    return or_(Notification.user_id == user.id, Notification.role == user.role)


async def _email_role(
    session: AsyncSession,
    *,
    role: Role,
    subject: str,
    body: str,
    settings: Settings | None,
) -> int:
    """Send the courtesy copy to every active holder of a role. Returns how many were accepted."""
    cfg = settings or get_settings()

    recipients = list(
        (
            await session.scalars(
                select(User.email).where(User.role == role, User.is_active.is_(True))
            )
        ).all()
    )
    if not recipients:
        log.info("email_no_recipients", role=role.value)
        return 0

    sent = 0
    for address in recipients:
        if await send_email(cfg, to=address, subject=subject, body=body):
            sent += 1

    log.info("notification_emails", role=role.value, recipients=len(recipients), sent=sent)
    return sent
