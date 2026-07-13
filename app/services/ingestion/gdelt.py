"""GDELT 2.0 DOC API — global news coverage matching the ministry's watch terms.

No API key. The catch is that GDELT does not use HTTP status codes to report failure: a
rate-limited request, a query it could not parse, or a term it considers too broad all come back
as **HTTP 200 with a plain-text body** — or with an empty body. `response.json()` on that raises
`json.JSONDecodeError`, which is a `ValueError`, which is exactly the kind of exception that reads
like a bug in our own code when it surfaces in a log three weeks later.

So the body is fetched as text and parsed by hand, and every non-JSON answer is turned into an
`IngestionError` that names the likely cause. The source ends up `failing` in `GET /sources` with
a sentence an operator can act on, instead of a stack trace.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Final

import structlog

from app.enums import Classification, ConnectorKind
from app.exceptions import IngestionError
from app.services.ingestion import config_int, config_str, config_str_list
from app.services.ingestion.base_connector import BaseConnector, RawItem

log = structlog.get_logger(__name__)

GDELT_DOC_API: Final = "https://api.gdeltproject.org/api/v2/doc/doc"

DEFAULT_MAX_RECORDS: Final = 60
DEFAULT_TIMESPAN: Final = "24h"


class GdeltConnector(BaseConnector):
    """Article-list mode: one `RawItem` per article GDELT saw in the window."""

    kind = ConnectorKind.GDELT

    async def fetch(self, since: datetime | None = None) -> list[RawItem]:
        """Pull articles matching the configured query terms.

        `since` is deliberately unused. The runner advances `last_synced_at` even when a sync
        fails, so narrowing the window against it would silently drop exactly the articles the
        failed poll missed. The configured `timespan` is the source's own recency policy, and
        re-serving articles we already hold is free — the runner's dedup discards them.
        """
        terms = config_str_list(self.config, "query_terms")
        if not terms:
            raise IngestionError(
                "GDELT source has no 'query_terms' in its config.",
                code="source_misconfigured",
                detail={"source_id": str(self.source_id)},
            )

        # Quoted so multi-word terms ("supply chain") stay phrases rather than becoming an
        # implicit AND of their words, which is a materially different query.
        query = " OR ".join(f'"{term}"' for term in terms)
        url = config_str(self.config, "base_url", GDELT_DOC_API)

        body = await self.get_text(
            url,
            params={
                "query": query,
                "mode": "ArtList",
                "format": "json",
                "maxrecords": config_int(self.config, "max_records", DEFAULT_MAX_RECORDS),
                "timespan": config_str(self.config, "timespan", DEFAULT_TIMESPAN),
            },
        )

        articles = self._parse(body)
        items = [item for article in articles if (item := self._to_item(article)) is not None]

        log.info(
            "gdelt_fetched",
            source_id=str(self.source_id),
            terms=len(terms),
            articles=len(articles),
            items=len(items),
        )
        return items

    def _parse(self, body: str) -> list[Any]:
        """Turn a GDELT 200 into article rows, or into an IngestionError that explains itself."""
        text = body.strip()
        if not text:
            raise IngestionError(
                "GDELT returned an empty body. This normally means the query was rejected as "
                "too broad, or the endpoint is rate-limiting us.",
                detail={"source_id": str(self.source_id)},
            )

        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            # The body is GDELT's own error prose, not source content — but it is still not worth
            # logging, so only its size is recorded.
            raise IngestionError(
                "GDELT returned HTTP 200 with a body that is not JSON. This is how it reports a "
                "rejected query or a rate limit.",
                detail={"source_id": str(self.source_id), "body_bytes": len(text)},
            ) from exc

        if not isinstance(payload, dict):
            raise IngestionError(
                "GDELT returned JSON that is not an object.",
                detail={"source_id": str(self.source_id)},
            )

        articles = payload.get("articles")
        if articles is None:
            # A window with no matching coverage is a legitimate, healthy answer — not an error.
            return []
        if not isinstance(articles, list):
            raise IngestionError(
                "GDELT returned an 'articles' field that is not a list.",
                detail={"source_id": str(self.source_id)},
            )
        return articles

    def _to_item(self, article: Any) -> RawItem | None:
        if not isinstance(article, dict):
            return None

        title = self.clean(_as_str(article.get("title")))
        url = _as_str(article.get("url"))
        # Without a URL there is no stable identity, so every poll would re-create the article as
        # a "new" item. Without a title there is nothing to triage. Either way, drop it.
        if not title or not url:
            return None

        domain = _as_str(article.get("domain"))
        return RawItem(
            title=title,
            external_id=url,
            summary=None if domain is None else f"Reported by {domain}.",
            url=url,
            published_at=self.parse_datetime(article.get("seendate")),
            # GDELT's own `language` field is a display name ("Arabic"), not a code. Detecting
            # from the title keeps this consistent with every other connector.
            language=self.detect_language(title),
            classification=Classification.PUBLIC,
            raw={
                "domain": domain,
                "seendate": _as_str(article.get("seendate")),
                "gdelt_language": _as_str(article.get("language")),
                "socialimage": _as_str(article.get("socialimage")),
            },
        )


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = ["GdeltConnector"]
