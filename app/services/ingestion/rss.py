"""Generic RSS / Atom connector.

Two things this connector does deliberately:

* **It fetches the body itself and hands `feedparser` a string.** `feedparser.parse(url)` does its
  own *synchronous* network I/O — inside an async worker that blocks the event loop for the whole
  round trip, stalling every other source, request and agent sharing it. Parsing a string keeps
  the network call on `httpx.AsyncClient` where it belongs.
* **One dead feed does not take the others down.** A source is configured with N feeds; a 404 on
  one of them must not cost us the items from the other N-1. Each feed is fetched inside its own
  error boundary, and a feed that fails is logged and skipped.

Summaries arrive as HTML (`<p>`, tracking pixels, "read more" anchors). They are stripped to text
here rather than downstream, so that what the Signal Agent triages, what the embedder embeds and
what the UI renders are all the same string.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

import feedparser
import structlog
from bs4 import BeautifulSoup

from app.enums import Classification, ConnectorKind
from app.exceptions import IngestionError
from app.services.ingestion import config_str_list
from app.services.ingestion.base_connector import BaseConnector, RawItem

log = structlog.get_logger(__name__)

# Feed summaries are teasers, not documents. Anything past this is boilerplate — publisher
# footers, subscription pitches — and only dilutes the embedding.
SUMMARY_CHAR_LIMIT: Final = 1_000
# Some publishers put the whole article in <content:encoded>. Keep it, but bounded.
CONTENT_CHAR_LIMIT: Final = 20_000


class RssConnector(BaseConnector):
    """One `RawItem` per entry, across every feed in `config["feeds"]`."""

    kind = ConnectorKind.RSS

    async def fetch(self, since: datetime | None = None) -> list[RawItem]:
        """Fetch and parse every configured feed.

        `since` is deliberately unused: a feed serves a fixed recent window and cannot be asked
        for "everything after T". Re-serving entries we already hold is free — the runner's dedup
        discards them — whereas filtering on `last_synced_at` (which the runner advances even on a
        failed sync) would silently drop the entries a failed poll missed.
        """
        feeds = config_str_list(self.config, "feeds")
        if not feeds:
            raise IngestionError(
                "RSS source has no 'feeds' in its config.",
                code="source_misconfigured",
                detail={"source_id": str(self.source_id)},
            )

        items: list[RawItem] = []
        failed = 0
        for feed_url in feeds:
            try:
                body = await self.get_text(feed_url)
            except IngestionError as exc:
                # The error boundary that keeps one dead feed from costing us the others.
                failed += 1
                log.warning(
                    "rss_feed_unavailable",
                    source_id=str(self.source_id),
                    feed_url=feed_url,
                    error_code=exc.code,
                )
                continue
            items.extend(self._parse_feed(body, feed_url))

        # Only when *every* feed failed is the source itself broken; anything less is partial
        # coverage, which is better than none.
        if failed == len(feeds):
            raise IngestionError(
                "Every configured RSS feed failed to fetch.",
                detail={"source_id": str(self.source_id), "feeds": failed},
            )

        log.info(
            "rss_fetched",
            source_id=str(self.source_id),
            feeds=len(feeds),
            failed_feeds=failed,
            items=len(items),
        )
        return items

    def _parse_feed(self, body: str, feed_url: str) -> list[RawItem]:
        parsed: Any = feedparser.parse(body)
        entries: Any = getattr(parsed, "entries", [])
        if not isinstance(entries, list):
            return []

        # `bozo` means the XML was malformed. feedparser still recovers entries from most broken
        # feeds, so this is only worth reporting when it actually cost us everything.
        if getattr(parsed, "bozo", False) and not entries:
            log.warning("rss_feed_unparseable", source_id=str(self.source_id), feed_url=feed_url)
            return []

        feed_title = _as_str(getattr(parsed, "feed", {}).get("title"))
        items: list[RawItem] = []
        for entry in entries:
            item = self._to_item(entry, feed_url, feed_title)
            if item is not None:
                items.append(item)
        return items

    def _to_item(self, entry: Any, feed_url: str, feed_title: str | None) -> RawItem | None:
        title = self.clean(_as_str(entry.get("title")))
        if not title:
            return None

        link = _as_str(entry.get("link"))
        summary = self.clean(
            _strip_html(_as_str(entry.get("summary")) or _as_str(entry.get("description"))),
            limit=SUMMARY_CHAR_LIMIT,
        )
        content = self.clean(_strip_html(_entry_content(entry)), limit=CONTENT_CHAR_LIMIT)
        published_at = self.parse_datetime(
            _as_str(entry.get("published")) or _as_str(entry.get("updated"))
        )

        return RawItem(
            title=title,
            # `entry.id` (Atom) / `<guid>` (RSS) is the publisher's own identifier and survives a
            # URL rewrite; the link is the fallback for the feeds that omit it.
            external_id=_as_str(entry.get("id")) or link,
            summary=summary,
            content=content,
            url=link,
            published_at=published_at,
            language=self.detect_language(f"{title} {summary or ''}"),
            classification=Classification.PUBLIC,
            # Only primitives: a feedparser entry carries `time.struct_time` objects, which JSONB
            # cannot serialise.
            raw={"feed_url": feed_url, "feed_title": feed_title},
        )


def _entry_content(entry: Any) -> str | None:
    """The full-text `<content:encoded>` body, when the publisher provides one."""
    content = entry.get("content")
    if not isinstance(content, list) or not content:
        return None
    first = content[0]
    if not isinstance(first, dict):
        return None
    return _as_str(first.get("value"))


def _strip_html(markup: str | None) -> str | None:
    """Feed summaries are HTML fragments. What we store must be text."""
    if not markup:
        return None
    text = BeautifulSoup(markup, "html.parser").get_text(" ", strip=True)
    return text or None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = ["RssConnector"]
