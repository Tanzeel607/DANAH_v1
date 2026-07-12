"""Embedding providers.

Both Voyage and OpenAI accept an explicit output dimension, so `EMBEDDING_DIM` is *honoured*
rather than merely asserted — which matters because the `vector(n)` column is fixed at
migration time and a mismatch would fail every insert.

Voyage is called over its REST API with `httpx` rather than through the `voyageai` SDK: the
architecture already mandates httpx for all outbound HTTP, and it keeps one fewer vendor SDK in
a codebase that will face government review.
"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from typing import Any, Protocol

import httpx
import structlog

from app.config import Settings, get_settings
from app.enums import EmbeddingProvider as ProviderName
from app.exceptions import LLMGatewayError, LLMNotConfiguredError

log = structlog.get_logger(__name__)

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


class Embedder(Protocol):
    """The interface the RAG layer depends on (and that tests fake)."""

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...

    @property
    def dimension(self) -> int: ...
    @property
    def model(self) -> str: ...
    @property
    def provider(self) -> str: ...


class BaseEmbedder(ABC):
    """Batching, retry and validation shared by every provider."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    @property
    def dimension(self) -> int:
        return self.settings.embedding_dim

    @property
    @abstractmethod
    def model(self) -> str: ...

    @property
    @abstractmethod
    def provider(self) -> str: ...

    @abstractmethod
    async def _embed_batch(self, texts: list[str], *, is_query: bool) -> list[list[float]]: ...

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds)
        return self._client

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed in batches of `EMBEDDING_BATCH_SIZE`, preserving input order."""
        if not texts:
            return []

        batch_size = max(1, self.settings.embedding_batch_size)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            vectors.extend(await self._with_retries(batch, is_query=False))

        self._validate(vectors, expected=len(texts))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._with_retries([text], is_query=True)
        self._validate(vectors, expected=1)
        return vectors[0]

    async def _with_retries(self, texts: list[str], *, is_query: bool) -> list[list[float]]:
        attempts = max(1, self.settings.llm_max_retries)
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                return await self._embed_batch(texts, is_query=is_query)
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
                if not retryable or attempt == attempts - 1:
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == attempts - 1:
                    break

            delay = min(2**attempt, 8) * (0.5 + random.random())  # noqa: S311 - jitter, not crypto
            log.warning(
                "embedding_retry",
                provider=self.provider,
                attempt=attempt + 1,
                batch=len(texts),
                delay_s=round(delay, 2),
            )
            await asyncio.sleep(delay)

        raise LLMGatewayError(
            "The embedding provider is unavailable after retries.",
            detail={"provider": self.provider, "model": self.model, "batch": len(texts)},
        ) from last_exc

    def _validate(self, vectors: list[list[float]], *, expected: int) -> None:
        if len(vectors) != expected:
            raise LLMGatewayError(
                "The embedding provider returned the wrong number of vectors.",
                detail={"expected": expected, "received": len(vectors)},
            )
        for vec in vectors:
            if len(vec) != self.dimension:
                # A silent dimension mismatch would fail at INSERT with an opaque pgvector
                # error; catching it here names the actual problem.
                raise LLMGatewayError(
                    "The embedding provider returned vectors of the wrong dimension.",
                    detail={
                        "expected_dim": self.dimension,
                        "received_dim": len(vec),
                        "model": self.model,
                        "hint": "EMBEDDING_DIM must match the model and the vector() column.",
                    },
                )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class VoyageEmbedder(BaseEmbedder):
    """Voyage AI (`voyage-3.5` by default)."""

    @property
    def model(self) -> str:
        return self.settings.embedding_model

    @property
    def provider(self) -> str:
        return ProviderName.VOYAGE.value

    async def _embed_batch(self, texts: list[str], *, is_query: bool) -> list[list[float]]:
        key = self.settings.voyage_api_key.get_secret_value()
        if not key:
            raise LLMNotConfiguredError(
                "VOYAGE_API_KEY is not set. Set it (or switch EMBEDDING_PROVIDER=openai) "
                "and restart. See FIRST_RUN.md."
            )

        payload: dict[str, Any] = {
            "input": texts,
            "model": self.model,
            # Voyage embeds queries and documents asymmetrically; using the right input_type
            # measurably improves retrieval over treating both the same.
            "input_type": "query" if is_query else "document",
            "output_dimension": self.dimension,
        }
        response = await self._http().post(
            VOYAGE_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        body = response.json()

        # Voyage does not guarantee ordering; sort by the index it returns.
        items = sorted(body["data"], key=lambda d: d["index"])
        return [item["embedding"] for item in items]


class OpenAIEmbedder(BaseEmbedder):
    """OpenAI (`text-embedding-3-small` by default).

    `dimensions` is passed explicitly so a 1536-native model can emit the configured 1024 and
    match the `vector(1024)` column.
    """

    @property
    def model(self) -> str:
        return self.settings.openai_embedding_model

    @property
    def provider(self) -> str:
        return ProviderName.OPENAI.value

    async def _embed_batch(self, texts: list[str], *, is_query: bool) -> list[list[float]]:
        key = self.settings.openai_api_key.get_secret_value()
        if not key:
            raise LLMNotConfiguredError(
                "OPENAI_API_KEY is not set but EMBEDDING_PROVIDER=openai. See FIRST_RUN.md."
            )

        payload: dict[str, Any] = {
            "input": texts,
            "model": self.model,
            "dimensions": self.dimension,
        }
        response = await self._http().post(
            OPENAI_EMBEDDINGS_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        body = response.json()

        items = sorted(body["data"], key=lambda d: d["index"])
        return [item["embedding"] for item in items]


_embedder: Embedder | None = None


def build_embedder(settings: Settings | None = None) -> Embedder:
    cfg = settings or get_settings()
    if cfg.embedding_provider is ProviderName.VOYAGE:
        return VoyageEmbedder(cfg)
    return OpenAIEmbedder(cfg)


def get_embedder() -> Embedder:
    """FastAPI dependency. Tests override this to inject `FakeEmbedder`."""
    global _embedder
    if _embedder is None:
        _embedder = build_embedder()
    return _embedder


def reset_embedder() -> None:
    global _embedder
    _embedder = None
