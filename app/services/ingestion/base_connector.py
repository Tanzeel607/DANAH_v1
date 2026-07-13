"""The connector contract.

Every source — an open API today, a licensed feed tomorrow — normalises to the same `RawItem`
and is deduplicated by the same rule. That is what lets Bloomberg or Reuters arrive later as
either a new `BaseConnector` subclass or a webhook producer, with **no schema change and no new
code path** (architecture §9).

Deduplication is the load-bearing part. Sources are polled on a schedule and re-serve the same
records, so without a stable identity every poll would duplicate the corpus and the Signal Agent
would triage the same item repeatedly, at cost.

    dedup_hash = sha256(source_id + (external_id | url | title + published_date))

The fallback chain matters: `external_id` is stable when a source provides one; a `url` is stable
for news; `title + date` is the last resort for feeds that provide neither, and is deliberately
date-scoped so a recurring headline ("Weekly Economic Update") on different days is *not*
collapsed into one item.
"""

from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.config import Settings, get_settings
from app.enums import Classification, ConnectorKind, Language
from app.exceptions import IngestionError

log = structlog.get_logger(__name__)

DEFAULT_USER_AGENT = "DANAH-StrategicIntelligence/0.4 (+government research; contact: ministry)"


@dataclass(slots=True)
class RawItem:
    """One normalised record, before it becomes an `ingested_items` row."""

    title: str
    external_id: str | None = None
    summary: str | None = None
    content: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    language: Language = Language.EN
    classification: Classification = Classification.PUBLIC
    raw: dict[str, Any] = field(default_factory=dict)

    def dedup_hash(self, source_id: uuid.UUID) -> str:
        """Stable identity for this item within its source."""
        if self.external_id:
            identity = self.external_id
        elif self.url:
            identity = self.url
        else:
            # Date-scoped, so a recurring headline on a new day is a new item, not a duplicate.
            day = self.published_at.date().isoformat() if self.published_at else "nodate"
            identity = f"{self.title.strip().lower()}|{day}"

        return hashlib.sha256(f"{source_id}:{identity}".encode()).hexdigest()


@dataclass(slots=True)
class FetchResult:
    items: list[RawItem]
    status: str = "ok"
    error: str | None = None


class BaseConnector(ABC):
    """Base for every source connector.

    Subclasses implement `fetch()`. Everything else — the HTTP client, the user agent, timeouts,
    error translation — is shared, so a new connector is genuinely small.
    """

    kind: ConnectorKind

    def __init__(
        self,
        source_id: uuid.UUID,
        config: dict[str, Any],
        settings: Settings | None = None,
    ) -> None:
        self.source_id = source_id
        self.config = config
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    @abstractmethod
    async def fetch(self, since: datetime | None = None) -> list[RawItem]:
        """Pull items from the source. `since` is `sources.last_synced_at`.

        Raise `IngestionError` for a source-side failure. Do NOT deduplicate here — the runner
        owns that, so every connector deduplicates identically.
        """

    # -- shared HTTP ---------------------------------------------------------
    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers={"User-Agent": DEFAULT_USER_AGENT},
                follow_redirects=True,
            )
        return self._client

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET returning parsed JSON, with source-side failures translated to IngestionError."""
        try:
            response = await self.http.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise IngestionError(
                f"{self.kind.value} returned HTTP {exc.response.status_code}.",
                detail={"url": url, "status": exc.response.status_code},
            ) from exc
        except httpx.TimeoutException as exc:
            raise IngestionError(f"{self.kind.value} timed out.", detail={"url": url}) from exc
        except (httpx.TransportError, ValueError) as exc:
            # ValueError covers a 200 carrying malformed JSON, which open APIs do under load.
            raise IngestionError(
                f"{self.kind.value} was unreachable or returned malformed data.",
                detail={"url": url, "error": str(exc)},
            ) from exc

    async def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        try:
            response = await self.http.get(url, params=params)
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError as exc:
            raise IngestionError(
                f"{self.kind.value} returned HTTP {exc.response.status_code}.",
                detail={"url": url, "status": exc.response.status_code},
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise IngestionError(
                f"{self.kind.value} was unreachable.", detail={"url": url, "error": str(exc)}
            ) from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def parse_datetime(value: Any) -> datetime | None:
        """Best-effort timestamp parsing across the formats open sources actually emit.

        A missing or unparseable date is not fatal — it degrades the dedup key to
        `title|nodate` and the item still ingests. Losing an item because a feed emitted a
        malformed date would be a worse outcome than a slightly weaker dedup key.
        """
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)

        text = str(value).strip()
        formats = (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%Y%m%dT%H%M%SZ",  # GDELT
            "%Y%m%d%H%M%S",
            "%a, %d %b %Y %H:%M:%S %z",  # RFC 822 (RSS)
            "%a, %d %b %Y %H:%M:%S %Z",
        )
        for fmt in formats:
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            log.debug("unparsed_datetime", value=text[:40])
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @staticmethod
    def clean(text: str | None, *, limit: int | None = None) -> str | None:
        if not text:
            return None
        cleaned = " ".join(str(text).split())
        if limit and len(cleaned) > limit:
            cleaned = cleaned[:limit].rstrip() + "…"
        return cleaned or None

    @staticmethod
    def detect_language(text: str) -> Language:
        letters = [c for c in text[:600] if c.isalpha()]
        if not letters:
            return Language.EN
        arabic = sum(1 for c in letters if "؀" <= c <= "ۿ")
        return Language.AR if arabic / len(letters) > 0.30 else Language.EN


def build_connector(
    connector: ConnectorKind,
    source_id: uuid.UUID,
    config: dict[str, Any],
    settings: Settings | None = None,
) -> BaseConnector:
    """Resolve a `sources.connector` value to its implementation."""
    from app.services.ingestion.gdelt import GdeltConnector
    from app.services.ingestion.reliefweb import ReliefWebConnector
    from app.services.ingestion.rss import RssConnector
    from app.services.ingestion.worldbank import WorldBankConnector

    registry: dict[ConnectorKind, type[BaseConnector]] = {
        ConnectorKind.WORLDBANK: WorldBankConnector,
        ConnectorKind.GDELT: GdeltConnector,
        ConnectorKind.RSS: RssConnector,
        ConnectorKind.RELIEFWEB: ReliefWebConnector,
    }

    implementation = registry.get(connector)
    if implementation is None:
        # CUSTOM sources exist to receive webhook pushes; they have nothing to poll.
        raise IngestionError(
            f"No polling connector is registered for '{connector.value}'.",
            code="unknown_connector",
            detail={
                "connector": connector.value,
                "hint": "custom sources receive data via POST /api/ingest/webhook/{source_id}",
            },
        )

    return implementation(source_id, config, settings)
