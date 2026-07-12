"""Grounded chat.

The whole Phase-1 acceptance criterion lives in this file: ask about an uploaded document and get
an answer with a citation pointing at it; ask about something outside the corpus and get an
explicit "not in my sources" rather than a confident invention.

The retrieval query is bounded by the caller's clearance, in SQL. An executive and a viewer
asking the identical question are answered from different corpora, and the viewer's prompt never
contains a single OFFICIAL-SENSITIVE token.
"""

from __future__ import annotations

import time
import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.enums import ChatRole, Language, UsagePurpose
from app.exceptions import NotFoundError, PermissionDeniedError
from app.models import ChatMessage, ChatSession, User
from app.schemas.chat import ChatResponse
from app.schemas.common import Citation
from app.security.rbac import user_clearance
from app.services.llm.gateway import LLMGateway
from app.services.rag import composer
from app.services.rag.embeddings import Embedder
from app.services.rag.retriever import Retriever

log = structlog.get_logger(__name__)

# How many prior turns to replay. Enough for pronouns and follow-ups ("what about the second
# one?") to resolve, small enough that the source block stays the dominant context.
HISTORY_TURNS = 6


async def answer(
    session: AsyncSession,
    *,
    user: User,
    message: str,
    session_id: uuid.UUID | None,
    language: Language | None,
    gateway: LLMGateway,
    embedder: Embedder,
    settings: Settings | None = None,
) -> ChatResponse:
    cfg = settings or get_settings()
    started = time.perf_counter()

    chat_session = await _resolve_session(
        session, user=user, session_id=session_id, message=message
    )

    answer_language = composer.detect_answer_language(message, language)
    ceiling = user_clearance(user)

    retriever = Retriever(embedder, cfg)
    chunks = await retriever.retrieve(
        session,
        message,
        k=cfg.retrieval_top_k,
        classification_ceiling=ceiling,
    )

    history = await _recent_history(session, chat_session.id)
    messages = [
        *history,
        {"role": "user", "content": composer.build_user_message(message, chunks)},
    ]

    result = await gateway.complete(
        messages,
        system=composer.system_prompt(answer_language),
        purpose=UsagePurpose.CHAT.value,
        user_id=user.id,
    )

    abstained = composer.is_abstention(result.text)
    citations: list[Citation] = [] if abstained else composer.build_citations(result.text, chunks)
    # A model can claim grounding while citing nothing; treat "no citations" as an abstention
    # regardless of what it said, so `grounded` never over-promises.
    if not citations:
        abstained = True

    text = composer.strip_abstain_marker(result.text) if abstained else result.text
    confidence = composer.compute_confidence(chunks, citations, abstained=abstained)

    latency_ms = int((time.perf_counter() - started) * 1000)

    assistant_message = await _persist_turn(
        session,
        chat_session=chat_session,
        question=message,
        answer_text=text,
        citations=citations,
        confidence=confidence,
        language=answer_language,
        usage_in=result.usage.input_tokens,
        usage_out=result.usage.output_tokens,
        latency_ms=latency_ms,
    )

    log.info(
        "chat_answered",
        session_id=str(chat_session.id),
        user_id=str(user.id),
        clearance=ceiling.value,
        retrieved=len(chunks),
        cited=len(citations),
        grounded=not abstained,
        confidence=confidence,
        latency_ms=latency_ms,
        # Neither the question nor the answer text is logged (§12).
    )

    return ChatResponse(
        session_id=chat_session.id,
        message_id=assistant_message.id,
        answer=text,
        citations=citations,
        confidence=confidence,
        grounded=not abstained,
        language=answer_language,
        latency_ms=latency_ms,
        tokens_in=result.usage.input_tokens,
        tokens_out=result.usage.output_tokens,
    )


async def _resolve_session(
    session: AsyncSession,
    *,
    user: User,
    session_id: uuid.UUID | None,
    message: str,
) -> ChatSession:
    if session_id is None:
        created = ChatSession(
            id=uuid.uuid4(),
            user_id=user.id,
            title=_title_from(message),
        )
        session.add(created)
        await session.flush()
        return created

    existing = await session.get(ChatSession, session_id)
    if existing is None or existing.user_id != user.id:
        # Another user's conversation is reported as "not found", not "forbidden": a 403 would
        # confirm that the id exists and belongs to someone.
        raise NotFoundError("Chat session not found.")
    return existing


def _title_from(message: str) -> str:
    condensed = " ".join(message.split())
    return condensed[:60] + "…" if len(condensed) > 60 else condensed or "New conversation"


async def _recent_history(session: AsyncSession, session_id: uuid.UUID) -> list[dict[str, str]]:
    """Replay the last few turns.

    Only the assistant's prose is replayed, not the source blocks from previous turns: re-sending
    them would blow the context window and let stale sources bleed into a new question's grounding.
    """
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(HISTORY_TURNS)
    )
    rows = list((await session.scalars(stmt)).all())
    rows.reverse()
    return [{"role": m.role.value, "content": m.content} for m in rows]


async def _persist_turn(
    session: AsyncSession,
    *,
    chat_session: ChatSession,
    question: str,
    answer_text: str,
    citations: list[Citation],
    confidence: float,
    language: Language,
    usage_in: int,
    usage_out: int,
    latency_ms: int,
) -> ChatMessage:
    """Write both sides of the turn and return the assistant row.

    The `chat_session.messages` relationship is deliberately not touched: appending to it would
    lazy-load the collection, which raises `MissingGreenlet` under async SQLAlchemy.
    """
    session.add(
        ChatMessage(
            id=uuid.uuid4(),
            session_id=chat_session.id,
            role=ChatRole.USER,
            content=question,
            citations=[],
            language=language,
        )
    )
    assistant = ChatMessage(
        id=uuid.uuid4(),
        session_id=chat_session.id,
        role=ChatRole.ASSISTANT,
        content=answer_text,
        citations=[c.model_dump(mode="json") for c in citations],
        confidence=confidence,
        language=language,
        tokens_in=usage_in,
        tokens_out=usage_out,
        latency_ms=latency_ms,
    )
    session.add(assistant)
    await session.flush()
    return assistant


async def list_sessions(session: AsyncSession, *, user: User) -> list[tuple[ChatSession, int]]:
    stmt = (
        select(ChatSession, func.count(ChatMessage.id))
        .outerjoin(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .where(ChatSession.user_id == user.id)
        .group_by(ChatSession.id)
        .order_by(ChatSession.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [(cs, int(count)) for cs, count in rows]


async def get_session_detail(
    session: AsyncSession, *, user: User, session_id: uuid.UUID
) -> tuple[ChatSession, list[ChatMessage]]:
    chat_session = await session.get(ChatSession, session_id)
    if chat_session is None:
        raise NotFoundError("Chat session not found.")
    if chat_session.user_id != user.id:
        raise PermissionDeniedError("This conversation belongs to another user.")

    messages = list(
        (
            await session.scalars(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at)
            )
        ).all()
    )
    return chat_session, messages
