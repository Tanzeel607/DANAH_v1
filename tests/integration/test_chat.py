"""Phase 1 acceptance criteria (master prompt §10).

  * chat about an uploaded document → an answer with ≥1 citation pointing at that document,
    and a confidence in [0, 1]
  * a question outside the corpus → an explicit "not in my sources" abstention
  * classification is enforced in retrieval, so a viewer's prompt cannot contain
    OFFICIAL-SENSITIVE text

These run against the real retriever, the real composer and the real API, with only the LLM and
the embedder faked at the gateway interface.
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Classification, DocumentStatus, Language, Role
from app.models import Document, DocumentChunk


async def _index_chunks(
    db: AsyncSession,
    fake_embedder: Any,
    *,
    title: str,
    paragraphs: list[str],
    classification: Classification,
) -> Document:
    doc = Document(
        id=uuid.uuid4(),
        title=title,
        filename=f"{title.lower().replace(' ', '-')}.md",
        mime_type="text/markdown",
        storage_path=f"test/{uuid.uuid4()}.md",
        language=Language.EN,
        classification=classification,
        status=DocumentStatus.INDEXED,
        chunk_count=len(paragraphs),
    )
    db.add(doc)
    await db.flush()

    vectors = await fake_embedder.embed_documents(paragraphs)
    for i, (text, vec) in enumerate(zip(paragraphs, vectors, strict=True)):
        db.add(
            DocumentChunk(
                id=uuid.uuid4(),
                document_id=doc.id,
                chunk_index=i,
                content=text,
                token_count=len(text.split()),
                embedding=vec,
                classification=classification,
                language=Language.EN,
                meta={},
            )
        )
    await db.commit()
    return doc


class TestGroundedChat:
    async def test_answer_cites_the_uploaded_document(
        self,
        client: AsyncClient,
        db: AsyncSession,
        fake_embedder: Any,
        auth_headers: Any,
    ) -> None:
        """§10 Phase 1: an answer about the document carries ≥1 citation pointing AT that document."""
        doc = await _index_chunks(
            db,
            fake_embedder,
            title="Ministry Strategic Plan 2026",
            paragraphs=[
                "The Ministry targets a non-oil GDP share of 65 percent by 2030.",
                "All OFFICIAL-SENSITIVE government workloads must remain within national borders.",
            ],
            classification=Classification.INTERNAL,
        )
        headers = await auth_headers(Role.ANALYST)

        resp = await client.post(
            "/api/agent/chat",
            json={"message": "What is the non-oil GDP target?"},
            headers=headers,
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["citations"], "a grounded answer must carry at least one citation"
        assert body["grounded"] is True
        assert any(c["document_id"] == str(doc.id) for c in body["citations"])
        assert 0.0 <= body["confidence"] <= 1.0
        assert body["confidence"] > 0.0
        assert body["session_id"]

    async def test_out_of_corpus_question_abstains(
        self,
        client: AsyncClient,
        db: AsyncSession,
        fake_embedder: Any,
        auth_headers: Any,
    ) -> None:
        """§10 Phase 1: outside the corpus → an explicit abstention, never an invention.

        The corpus is empty here, so the retriever returns nothing, the source block says so, and
        the model has nothing to cite.
        """
        headers = await auth_headers(Role.ANALYST)

        resp = await client.post(
            "/api/agent/chat",
            json={"message": "What is the capital city of Atlantis?"},
            headers=headers,
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["citations"] == []
        assert body["grounded"] is False
        # An abstention is confidently unhelpful, not confidently right.
        assert body["confidence"] == 0.0

    async def test_confidence_is_always_within_bounds(
        self,
        client: AsyncClient,
        db: AsyncSession,
        fake_embedder: Any,
        auth_headers: Any,
    ) -> None:
        await _index_chunks(
            db,
            fake_embedder,
            title="Trade Framework",
            paragraphs=["Concentration above 60 percent from one origin is a risk."],
            classification=Classification.INTERNAL,
        )
        headers = await auth_headers(Role.ANALYST)

        for question in ("What is the concentration threshold?", "Who won the 1974 World Cup?"):
            resp = await client.post("/api/agent/chat", json={"message": question}, headers=headers)

            assert resp.status_code == 200
            assert 0.0 <= resp.json()["confidence"] <= 1.0

    async def test_conversation_is_persisted(
        self,
        client: AsyncClient,
        db: AsyncSession,
        fake_embedder: Any,
        auth_headers: Any,
    ) -> None:
        await _index_chunks(
            db,
            fake_embedder,
            title="Strategy",
            paragraphs=["The diversification reserve is capped at 4 percent of expenditure."],
            classification=Classification.INTERNAL,
        )
        headers = await auth_headers(Role.ANALYST)

        first = await client.post(
            "/api/agent/chat", json={"message": "What is the reserve cap?"}, headers=headers
        )
        session_id = first.json()["session_id"]

        second = await client.post(
            "/api/agent/chat",
            json={"message": "And who approves exceptions?", "session_id": session_id},
            headers=headers,
        )
        assert second.status_code == 200
        assert second.json()["session_id"] == session_id

        detail = await client.get(f"/api/agent/chat/sessions/{session_id}", headers=headers)

        assert detail.status_code == 200
        messages = detail.json()["messages"]
        # Two turns: user + assistant, twice.
        assert len(messages) == 4
        assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]

    async def test_sessions_are_private_to_their_owner(
        self, client: AsyncClient, db: AsyncSession, fake_embedder: Any, auth_headers: Any
    ) -> None:
        owner = await auth_headers(Role.ANALYST)
        created = await client.post(
            "/api/agent/chat", json={"message": "First question"}, headers=owner
        )
        session_id = created.json()["session_id"]

        intruder = await auth_headers(Role.ANALYST)
        resp = await client.get(f"/api/agent/chat/sessions/{session_id}", headers=intruder)

        assert resp.status_code in (403, 404)

    async def test_chat_requires_authentication(self, client: AsyncClient) -> None:
        resp = await client.post("/api/agent/chat", json={"message": "hello"})

        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "auth_error"


class TestClassificationInRetrieval:
    async def test_viewer_cannot_retrieve_official_sensitive_content(
        self,
        client: AsyncClient,
        db: AsyncSession,
        fake_embedder: Any,
        auth_headers: Any,
    ) -> None:
        """The secret string must never reach a viewer's answer OR their citations.

        Clearance is a SQL filter, so the chunk is never even read for this caller
        (docs/DECISIONS.md #15).
        """
        secret = "The covert programme codename is NIGHTJAR and its budget is 40 million."
        await _index_chunks(
            db,
            fake_embedder,
            title="Sensitive Annex",
            paragraphs=[secret],
            classification=Classification.OFFICIAL_SENSITIVE,
        )
        viewer = await auth_headers(Role.VIEWER)

        resp = await client.post(
            "/api/agent/chat",
            json={"message": "What is the covert programme codename?"},
            headers=viewer,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert "NIGHTJAR" not in body["answer"]
        assert body["citations"] == []
        assert body["grounded"] is False

    async def test_executive_can_retrieve_official_sensitive_content(
        self,
        client: AsyncClient,
        db: AsyncSession,
        fake_embedder: Any,
        auth_headers: Any,
    ) -> None:
        """The negative test above only means something if the positive case works."""
        await _index_chunks(
            db,
            fake_embedder,
            title="Sensitive Annex",
            paragraphs=["The covert programme codename is NIGHTJAR."],
            classification=Classification.OFFICIAL_SENSITIVE,
        )
        executive = await auth_headers(Role.EXECUTIVE)

        resp = await client.post(
            "/api/agent/chat",
            json={"message": "What is the covert programme codename?"},
            headers=executive,
        )

        assert resp.status_code == 200
        assert resp.json()["citations"], "an executive holds OFFICIAL_SENSITIVE clearance"
