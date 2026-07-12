"""Grounded chat API (§7.7 #4-5). Mounted at /api/agent."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_config, get_current_user, get_db
from app.models import User
from app.schemas.chat import (
    ChatMessageOut,
    ChatRequest,
    ChatResponse,
    ChatSessionDetail,
    ChatSessionOut,
)
from app.schemas.common import Citation
from app.services import chat_service
from app.services.llm.gateway import LLMGateway, get_gateway
from app.services.rag.embeddings import Embedder, get_embedder

router = APIRouter(tags=["chat"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Ask a question, grounded in the document corpus",
    description=(
        "Retrieves from the knowledge base **within the caller's clearance**, then answers with "
        "numbered citations and a confidence score. If the corpus does not support an answer, the "
        "assistant abstains explicitly (`grounded: false`, `confidence: 0`) rather than inventing "
        "one.\n\n"
        "Omit `session_id` to start a new conversation; the response returns the new id."
    ),
)
async def chat(
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    gateway: LLMGateway = Depends(get_gateway),
    embedder: Embedder = Depends(get_embedder),
    settings: Settings = Depends(get_config),
) -> ChatResponse:
    return await chat_service.answer(
        db,
        user=user,
        message=payload.message,
        session_id=payload.session_id,
        language=payload.language,
        gateway=gateway,
        embedder=embedder,
        settings=settings,
    )


@router.get(
    "/chat/sessions",
    response_model=list[ChatSessionOut],
    summary="List the caller's chat sessions",
)
async def list_sessions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ChatSessionOut]:
    rows = await chat_service.list_sessions(db, user=user)
    return [
        ChatSessionOut(
            id=chat_session.id,
            title=chat_session.title,
            created_at=chat_session.created_at,
            message_count=count,
        )
        for chat_session, count in rows
    ]


@router.get(
    "/chat/sessions/{session_id}",
    response_model=ChatSessionDetail,
    summary="Full transcript of one chat session",
)
async def get_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChatSessionDetail:
    chat_session, messages = await chat_service.get_session_detail(
        db, user=user, session_id=session_id
    )
    return ChatSessionDetail(
        id=chat_session.id,
        title=chat_session.title,
        created_at=chat_session.created_at,
        message_count=len(messages),
        messages=[
            ChatMessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                citations=[Citation.model_validate(c) for c in m.citations],
                confidence=m.confidence,
                created_at=m.created_at,
            )
            for m in messages
        ],
    )
