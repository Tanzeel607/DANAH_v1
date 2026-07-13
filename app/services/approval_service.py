"""The publication gate.

This is the most important file in DANAH, and it is important because of what it refuses to do.

**An agent can only ever create a draft. Only a human decision publishes anything.**

That invariant is not a policy in a prompt, and it is not a convention the orchestrator is trusted
to follow. It is structural: `PublicationStatus.PUBLISHED` is written in exactly one place in the
entire codebase — inside `decide()`, on the branch reached only by a `decided_by` user id that
came from an authenticated request. `submit_for_approval()`, the only entry point an agent or the
orchestrator can reach, can move a subject to `pending_approval` and nowhere else. There is no
argument, no flag and no code path by which an agent reaches the published state; a language model
cannot talk its way past a function that does not exist.

Two supporting rules make it hold under real conditions:

* **A decision is final.** `decide()` rejects a second decision on an already-decided approval
  (409). Without that, a re-submitted subject could silently overwrite a human's rejection.
* **An agent cannot retract a human's approval.** Re-submitting an already-approved subject leaves
  it published and leaves the approval row alone, rather than dragging it back to `pending`.

Every decision is written to the hash-chained audit log with the deciding user and their IP.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    ActorType,
    AgentName,
    ApprovalStatus,
    ApprovalSubject,
    PublicationStatus,
    Role,
)
from app.exceptions import ApprovalError, InvalidRequestError, NotFoundError
from app.metrics import APPROVAL_DECISIONS
from app.models import Approval, Briefing, Insight, User
from app.schemas.approvals import ApprovalDecisionResponse, ApprovalOut
from app.services.audit_service import record_audit
from app.services.notification_service import notify_approval_pending, notify_briefing_published

log = structlog.get_logger(__name__)

# How much of the subject's body the queue shows. Enough to decide whether to open it, short
# enough that fifty rows are one small response.
SUMMARY_CHARS: Final[int] = 280

# A decision maps to exactly one publication state. `changes_requested` deliberately maps back to
# `pending_approval`: the subject stays out of sight and stays in the queue, awaiting a revision.
DECISION_OUTCOMES: Final[dict[str, tuple[ApprovalStatus, PublicationStatus]]] = {
    "approved": (ApprovalStatus.APPROVED, PublicationStatus.PUBLISHED),
    "rejected": (ApprovalStatus.REJECTED, PublicationStatus.REJECTED),
    "changes_requested": (ApprovalStatus.CHANGES_REQUESTED, PublicationStatus.PENDING_APPROVAL),
}

Subject = Insight | Briefing


async def submit_for_approval(
    session: AsyncSession,
    *,
    subject_type: ApprovalSubject,
    subject_id: uuid.UUID,
    requested_by_agent: AgentName,
    assigned_role: Role = Role.EXECUTIVE,
) -> Approval:
    """Put an agent's draft in front of a human. The highest state this can reach is
    `pending_approval` — never `published`.

    Idempotent. `(subject_type, subject_id)` is unique, so a re-run of the same step, or a
    re-submission after `changes_requested`, reuses the existing row instead of racing it: the
    insert is `ON CONFLICT DO NOTHING` and the row is then read back.
    """
    subject = await _resolve_subject(session, subject_type, subject_id)

    stmt = (
        pg_insert(Approval)
        .values(
            id=uuid.uuid4(),
            subject_type=subject_type,
            subject_id=subject_id,
            requested_by_agent=requested_by_agent,
            assigned_role=assigned_role,
            status=ApprovalStatus.PENDING,
        )
        .on_conflict_do_nothing(constraint="uq_approvals_subject")
        .returning(Approval.id)
    )
    # None means the row already existed: ON CONFLICT DO NOTHING returns nothing on conflict.
    inserted_id: uuid.UUID | None = await session.scalar(stmt)

    approval: Approval | None = await session.scalar(
        select(Approval).where(
            Approval.subject_type == subject_type,
            Approval.subject_id == subject_id,
        )
    )
    if approval is None:  # pragma: no cover - the insert above guarantees a row
        raise ApprovalError(
            "The approval row could not be created.",
            detail={"subject_type": subject_type.value, "subject_id": str(subject_id)},
        )

    if approval.status is ApprovalStatus.APPROVED:
        # A human published this. An agent re-running its step must not be able to pull it back
        # into the queue — that would let a repeated pipeline run un-publish approved work.
        log.warning(
            "approval_resubmit_ignored_already_approved",
            approval_id=str(approval.id),
            subject_type=subject_type.value,
            subject_id=str(subject_id),
        )
        return approval

    reopened = approval.status is not ApprovalStatus.PENDING
    approval.status = ApprovalStatus.PENDING
    subject.status = PublicationStatus.PENDING_APPROVAL
    await session.flush()

    log.info(
        "approval_submitted",
        approval_id=str(approval.id),
        subject_type=subject_type.value,
        subject_id=str(subject_id),
        requested_by_agent=requested_by_agent.value,
        assigned_role=assigned_role.value,
        created=inserted_id is not None,
        reopened=reopened,
    )

    # Only announce work that is genuinely new to the queue. Re-submitting an already-pending
    # subject (an orchestrator retry) must not re-notify every executive.
    if inserted_id is not None or reopened:
        await notify_approval_pending(
            session,
            subject_type=subject_type,
            subject_id=subject_id,
            title=_title(subject),
        )

    return approval


async def decide(
    session: AsyncSession,
    *,
    approval_id: uuid.UUID,
    user: User,
    decision: str,
    comment: str,
    ip: str | None,
) -> ApprovalDecisionResponse:
    """Record a human decision and apply it to the subject.

    The ONLY path in DANAH that writes `PublicationStatus.PUBLISHED`. `user` comes from an
    authenticated request (the route requires executive+), so a published row always has a real
    person's id against it in `decided_by` and in the audit chain.
    """
    outcome = DECISION_OUTCOMES.get(decision)
    if outcome is None:
        raise InvalidRequestError(
            "Unknown decision.",
            detail={"decision": decision, "allowed": sorted(DECISION_OUTCOMES)},
        )
    approval_status, subject_status = outcome

    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise NotFoundError("Approval not found.")

    if approval.status is not ApprovalStatus.PENDING:
        # A second decision must not silently overwrite the first. The queue is stale; reload it.
        raise ApprovalError(
            f"This item was already decided ({approval.status.value}).",
            detail={
                "approval_id": str(approval_id),
                "current_status": approval.status.value,
                "attempted": decision,
            },
        )

    subject = await _resolve_subject(session, approval.subject_type, approval.subject_id)

    decided_at = datetime.now(UTC)
    approval.status = approval_status
    approval.decided_by = user.id
    approval.decided_at = decided_at
    approval.comment = comment or None
    subject.status = subject_status
    await session.flush()

    await record_audit(
        session,
        action="approval.decision",
        actor_type=ActorType.USER,
        actor_id=user.id,
        subject_type=approval.subject_type.value,
        subject_id=approval.subject_id,
        ip=ip,
        detail={
            "approval_id": str(approval.id),
            "decision": decision,
            "subject_status": subject_status.value,
            "requested_by_agent": approval.requested_by_agent.value,
            "assigned_role": approval.assigned_role.value,
            "decided_by_role": user.role.value,
            # The comment itself lives on the approval row; the audit entry records that a
            # decision happened and who made it, not the free text of it.
            "comment_chars": len(comment),
        },
    )

    APPROVAL_DECISIONS.labels(subject_type=approval.subject_type.value, decision=decision).inc()

    if (
        approval_status is ApprovalStatus.APPROVED
        and approval.subject_type is ApprovalSubject.BRIEFING
    ):
        await notify_briefing_published(
            session, briefing_id=approval.subject_id, title=_title(subject)
        )

    log.info(
        "approval_decided",
        approval_id=str(approval.id),
        decision=decision,
        subject_type=approval.subject_type.value,
        subject_id=str(approval.subject_id),
        subject_status=subject_status.value,
        decided_by=str(user.id),
        role=user.role.value,
    )

    return ApprovalDecisionResponse(
        id=approval.id,
        status=approval.status,
        subject_type=approval.subject_type,
        subject_id=approval.subject_id,
        subject_status=subject_status.value,
        decided_by=user.id,
        decided_at=decided_at,
    )


async def list_pending(
    session: AsyncSession,
    *,
    status: ApprovalStatus | None,
    limit: int,
    offset: int,
) -> list[ApprovalOut]:
    """The approval queue, with each subject's headline already attached.

    The insight and briefing tables are LEFT JOINed on the polymorphic `subject_id` in the same
    query, so a fifty-row queue is one round trip rather than fifty-one.
    """
    stmt = (
        select(
            Approval,
            Insight.title.label("insight_title"),
            Insight.body.label("insight_body"),
            Insight.confidence.label("insight_confidence"),
            Insight.severity.label("insight_severity"),
            Briefing.title.label("briefing_title"),
            Briefing.body_en.label("briefing_body"),
            Briefing.confidence.label("briefing_confidence"),
        )
        .outerjoin(
            Insight,
            and_(
                Approval.subject_type == ApprovalSubject.INSIGHT,
                Insight.id == Approval.subject_id,
            ),
        )
        .outerjoin(
            Briefing,
            and_(
                Approval.subject_type == ApprovalSubject.BRIEFING,
                Briefing.id == Approval.subject_id,
            ),
        )
        .order_by(Approval.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(Approval.status == status)

    rows = (await session.execute(stmt)).all()

    queue: list[ApprovalOut] = []
    for (
        approval,
        insight_title,
        insight_body,
        insight_confidence,
        insight_severity,
        briefing_title,
        briefing_body,
        briefing_confidence,
    ) in rows:
        is_insight = approval.subject_type is ApprovalSubject.INSIGHT
        title = insight_title if is_insight else briefing_title
        body = insight_body if is_insight else briefing_body
        confidence = insight_confidence if is_insight else briefing_confidence

        queue.append(
            ApprovalOut(
                id=approval.id,
                subject_type=approval.subject_type,
                subject_id=approval.subject_id,
                requested_by_agent=approval.requested_by_agent,
                assigned_role=approval.assigned_role,
                status=approval.status,
                decided_by=approval.decided_by,
                decided_at=approval.decided_at,
                comment=approval.comment,
                created_at=approval.created_at,
                subject_title=title or "",
                subject_summary=_summarise(body),
                subject_confidence=float(confidence) if confidence is not None else None,
                # Only insights carry a severity; a briefing's queue row leaves it null rather
                # than inventing a number for the UI to sort on.
                subject_severity=int(insight_severity) if is_insight and insight_severity else None,
            )
        )

    return queue


async def count_approvals(session: AsyncSession, *, status: ApprovalStatus | None) -> int:
    """Total rows matching the queue filter, for the `Page.total` envelope."""
    stmt = select(func.count(Approval.id)).select_from(Approval)
    if status is not None:
        stmt = stmt.where(Approval.status == status)

    return int(await session.scalar(stmt) or 0)


async def approval_for_subject(
    session: AsyncSession,
    *,
    subject_type: ApprovalSubject,
    subject_id: uuid.UUID,
) -> Approval | None:
    """The approval attached to one subject — what `GET /api/insights/{id}` shows as its status."""
    approval: Approval | None = await session.scalar(
        select(Approval).where(
            Approval.subject_type == subject_type,
            Approval.subject_id == subject_id,
        )
    )
    return approval


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
async def _resolve_subject(
    session: AsyncSession,
    subject_type: ApprovalSubject,
    subject_id: uuid.UUID,
) -> Subject:
    """Load the insight or briefing an approval points at.

    `approvals.subject_id` is a polymorphic reference with no foreign key (it addresses two
    tables), so this is where referential integrity is actually enforced: an approval is never
    written, and never decided, for a subject that does not exist.
    """
    subject: Subject | None
    if subject_type is ApprovalSubject.INSIGHT:
        subject = await session.get(Insight, subject_id)
    else:
        subject = await session.get(Briefing, subject_id)

    if subject is None:
        raise NotFoundError(
            f"The {subject_type.value} this approval refers to no longer exists.",
            detail={"subject_type": subject_type.value, "subject_id": str(subject_id)},
        )
    return subject


def _title(subject: Subject) -> str:
    return subject.title


def _summarise(body: str | None) -> str:
    if not body:
        return ""
    condensed = " ".join(body.split())
    if len(condensed) <= SUMMARY_CHARS:
        return condensed
    return condensed[:SUMMARY_CHARS].rstrip() + "…"
