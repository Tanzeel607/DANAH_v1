"""ReliefWeb API v1 — humanitarian situation reports for the watched countries.

No API key; an `appname` identifies the caller.

The query goes as a **POST with a JSON body** rather than as the bracketed query string
(`?filter[field]=country.iso3&filter[value][]=ARE`). Both are supported by ReliefWeb, but the
bracket syntax has to be assembled by hand — `httpx` will not encode a nested filter for us — and
it is the form the ReliefWeb documentation covers least. A JSON body is what the API's own
examples use, and it is the only form in which a multi-value filter is unambiguous.

`BaseConnector` only exposes GET helpers, so the POST and its error translation live here, mapping
the same transport failures onto the same `IngestionError` the GET helpers raise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

import httpx
import structlog

from app.enums import Classification, ConnectorKind
from app.exceptions import IngestionError
from app.services.ingestion import config_int, config_str, config_str_list
from app.services.ingestion.base_connector import BaseConnector, RawItem

log = structlog.get_logger(__name__)

RELIEFWEB_REPORTS_API: Final = "https://api.reliefweb.int/v1/reports"
# ReliefWeb asks every caller to identify itself so it can contact us about a misbehaving client.
RELIEFWEB_APPNAME: Final = "danah"

DEFAULT_LIMIT: Final = 40
# Situation reports run to tens of pages. The summary is what the Signal Agent triages on, so it
# is the report's opening — the section that states what happened.
SUMMARY_CHAR_LIMIT: Final = 1_500
# The full body is kept for retrieval, but bounded: a single unbounded report can otherwise
# dominate a whole ingestion batch's memory and row size.
BODY_CHAR_LIMIT: Final = 50_000

# ISO3 is what `country.iso3` filters on. A 2-letter code silently matches nothing, which looks
# exactly like "no reports this week" — so it is worth a warning.
ISO3_LENGTH: Final = 3


class ReliefWebConnector(BaseConnector):
    """One `RawItem` per report, filtered to the configured countries."""

    kind = ConnectorKind.RELIEFWEB

    async def fetch(self, since: datetime | None = None) -> list[RawItem]:
        """Pull the latest reports for the configured countries.

        `since` is deliberately unused: `preset: latest` already bounds the request to the newest
        reports, and re-serving reports we already hold is free — the runner's dedup discards them
        on their stable ReliefWeb id.
        """
        countries = config_str_list(self.config, "countries")
        if not countries:
            raise IngestionError(
                "ReliefWeb source has no 'countries' in its config.",
                code="source_misconfigured",
                detail={"source_id": str(self.source_id)},
            )

        iso3 = [c.upper() for c in countries]
        suspect = [c for c in iso3 if len(c) != ISO3_LENGTH]
        if suspect:
            log.warning(
                "reliefweb_non_iso3_country",
                source_id=str(self.source_id),
                codes=suspect,
                filter_field="country.iso3",
            )

        payload = await self._post(
            config_str(self.config, "base_url", RELIEFWEB_REPORTS_API),
            {
                "limit": config_int(self.config, "limit", DEFAULT_LIMIT),
                "profile": "full",
                "preset": "latest",
                "filter": {"field": "country.iso3", "value": iso3},
                "fields": {
                    "include": [
                        "title",
                        "body",
                        "url",
                        "date.created",
                        "source.name",
                        "country.name",
                    ]
                },
            },
        )

        entries = payload.get("data")
        if not isinstance(entries, list):
            raise IngestionError(
                "ReliefWeb returned a response with no 'data' array.",
                detail={"source_id": str(self.source_id)},
            )

        items = [item for entry in entries if (item := self._to_item(entry)) is not None]
        log.info(
            "reliefweb_fetched",
            source_id=str(self.source_id),
            countries=len(iso3),
            entries=len(entries),
            items=len(items),
        )
        return items

    async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST a JSON query, translating transport failures the way the GET helpers do."""
        try:
            response = await self.http.post(url, params={"appname": RELIEFWEB_APPNAME}, json=body)
            response.raise_for_status()
            payload: Any = response.json()
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

        if not isinstance(payload, dict):
            raise IngestionError(
                f"{self.kind.value} returned JSON that is not an object.",
                detail={"url": url},
            )
        return payload

    def _to_item(self, entry: Any) -> RawItem | None:
        if not isinstance(entry, dict):
            return None
        raw_fields = entry.get("fields")
        fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}

        title = self.clean(_as_str(fields.get("title")))
        # ReliefWeb serves the id as a JSON number in some responses and as a string in others.
        external_id = _as_str(entry.get("id"))
        # No title means nothing to triage; no id means every poll would re-create the report.
        if not title or not external_id:
            return None

        body = self.clean(_as_str(fields.get("body")), limit=BODY_CHAR_LIMIT)
        summary = self.clean(body, limit=SUMMARY_CHAR_LIMIT)
        countries = _names(fields.get("country"))
        sources = _names(fields.get("source"))

        return RawItem(
            title=title,
            external_id=external_id,
            summary=summary,
            content=body,
            url=_as_str(fields.get("url")),
            published_at=self.parse_datetime(_date_created(fields)),
            language=self.detect_language(f"{title} {summary or ''}"),
            classification=Classification.PUBLIC,
            raw={
                "reliefweb_id": external_id,
                "countries": countries,
                "sources": sources,
            },
        )


def _date_created(fields: dict[str, Any]) -> str | None:
    date = fields.get("date")
    if not isinstance(date, dict):
        return None
    return _as_str(date.get("created"))


def _names(value: Any) -> list[str]:
    """ReliefWeb returns `source` and `country` as lists of objects, occasionally as one object."""
    entries = value if isinstance(value, list) else [value]
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            name = _as_str(entry.get("name"))
            if name:
                names.append(name)
    return names


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


__all__ = ["ReliefWebConnector"]
