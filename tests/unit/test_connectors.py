"""Connector unit tests against recorded responses. No test here touches the network.

Every payload below is the shape the real API actually serves, including the shapes that are
*wrong* — World Bank's null-valued years and its HTTP-200 error document, GDELT's HTTP-200
plain-text error body, a feed that 404s. Those are the cases that break ingestion in production,
so they are the cases worth recording.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from app.enums import Classification, Language
from app.exceptions import IngestionError
from app.services.ingestion.base_connector import RawItem
from app.services.ingestion.gdelt import GdeltConnector
from app.services.ingestion.reliefweb import ReliefWebConnector
from app.services.ingestion.rss import RssConnector
from app.services.ingestion.webhook import SIGNATURE_PREFIX, verify_signature
from app.services.ingestion.worldbank import WorldBankConnector

SOURCE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_SOURCE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

FEED_A = "https://example.gov/feed-a.xml"
FEED_B = "https://example.gov/feed-b.xml"


# ---------------------------------------------------------------------------
# Recorded payloads
# ---------------------------------------------------------------------------
def _wb_row(year: str, value: float | None) -> dict[str, Any]:
    return {
        "indicator": {"id": "NY.GDP.MKTP.KD.ZG", "value": "GDP growth (annual %)"},
        "country": {"id": "AE", "value": "United Arab Emirates"},
        "countryiso3code": "ARE",
        "date": year,
        "value": value,
        "unit": "",
        "obs_status": "",
        "decimal": 1,
    }


# `[metadata, [rows]]` — the two-element list the API serves on success. 2026 has no figure yet,
# which the World Bank represents as a row with a null value rather than as no row at all.
WORLDBANK_PAYLOAD: list[Any] = [
    {"page": 1, "pages": 1, "per_page": 6, "total": 4, "sourceid": "2"},
    [
        _wb_row("2026", None),
        _wb_row("2025", None),
        _wb_row("2024", 3.4),
        _wb_row("2023", 2.7),
        _wb_row("2022", 7.9),
    ],
]

# What the API returns for an indicator code that does not exist — with HTTP 200, and as a
# *one*-element list, so `payload[1]` would be an IndexError.
WORLDBANK_ERROR_PAYLOAD: list[Any] = [
    {
        "message": [
            {"id": "120", "key": "Invalid value", "value": "The provided parameter is not valid"}
        ]
    }
]

GDELT_PAYLOAD: dict[str, Any] = {
    "articles": [
        {
            "url": "https://news.example.com/uae-trade-policy",
            "url_mobile": "",
            "title": "UAE announces new trade policy framework",
            "seendate": "20260713T104500Z",
            "socialimage": "https://news.example.com/img/1.jpg",
            "domain": "news.example.com",
            "language": "English",
            "sourcecountry": "United Arab Emirates",
        },
        {
            "url": "https://akhbar.example.com/energy",
            "title": "ارتفاع أسعار الطاقة في الأسواق العالمية",
            "seendate": "20260713T090000Z",
            "socialimage": "",
            "domain": "akhbar.example.com",
            "language": "Arabic",
            "sourcecountry": "United Arab Emirates",
        },
    ]
}

RSS_FEED_A = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Business News</title>
    <item>
      <title>Supply chain disruption hits regional ports</title>
      <link>https://example.gov/articles/ports</link>
      <guid isPermaLink="false">example-guid-001</guid>
      <description><![CDATA[<p>Container throughput <b>fell 12%</b>.</p>
        <a href="https://example.gov/more">Read more</a>]]></description>
      <pubDate>Mon, 13 Jul 2026 08:30:00 +0000</pubDate>
    </item>
    <item>
      <title>Central bank holds rates</title>
      <link>https://example.gov/articles/rates</link>
      <description>Policy rate unchanged at 4.5%.</description>
      <pubDate>Sun, 12 Jul 2026 16:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

RSS_FEED_B = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Energy Wire</title>
    <item>
      <title>Gas prices climb on cold snap</title>
      <link>https://example.gov/articles/gas</link>
      <description>Benchmark futures rose.</description>
      <pubDate>Mon, 13 Jul 2026 07:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

RELIEFWEB_PAYLOAD: dict[str, Any] = {
    "time": 12,
    "totalCount": 2,
    "count": 2,
    "data": [
        {
            "id": "4098765",
            "score": 1,
            "fields": {
                "title": "Pakistan: Monsoon Floods Situation Report No. 4",
                "body": "Heavy monsoon rainfall has affected 2.1 million people across Sindh.",
                "url": "https://reliefweb.int/report/pakistan/monsoon-4",
                "date": {"created": "2026-07-12T14:20:00+00:00"},
                "source": [{"name": "OCHA"}],
                "country": [{"name": "Pakistan"}],
            },
        },
        {
            "id": 4098766,
            "score": 1,
            "fields": {
                "title": "United Arab Emirates: Humanitarian Aid Dispatch",
                "body": "The UAE dispatched 120 tonnes of relief supplies.",
                "url": "https://reliefweb.int/report/are/aid-dispatch",
                "date": {"created": "2026-07-11T09:00:00+00:00"},
                "source": [{"name": "UAE Red Crescent"}],
                "country": [{"name": "United Arab Emirates"}],
            },
        },
    ],
}


# ---------------------------------------------------------------------------
# World Bank
# ---------------------------------------------------------------------------
class TestWorldBank:
    def _connector(self, **overrides: Any) -> WorldBankConnector:
        config: dict[str, Any] = {
            "countries": ["ARE"],
            "indicators": ["NY.GDP.MKTP.KD.ZG"],
            "recent_years": 5,
        }
        config.update(overrides)
        return WorldBankConnector(SOURCE_ID, config)

    @respx.mock
    async def test_builds_items_an_analyst_could_read(self) -> None:
        respx.get(url__regex=r"https://api\.worldbank\.org/.*").mock(
            return_value=httpx.Response(200, json=WORLDBANK_PAYLOAD)
        )
        connector = self._connector()

        items = await connector.fetch()
        await connector.aclose()

        latest = next(i for i in items if i.external_id == "ARE:NY.GDP.MKTP.KD.ZG:2024")
        assert latest.title == "United Arab Emirates — GDP growth (annual %): 3.4 in 2024"
        assert latest.classification is Classification.PUBLIC
        assert latest.published_at is not None
        assert latest.published_at.year == 2024

    @respx.mock
    async def test_summary_states_the_year_on_year_change(self) -> None:
        respx.get(url__regex=r"https://api\.worldbank\.org/.*").mock(
            return_value=httpx.Response(200, json=WORLDBANK_PAYLOAD)
        )
        connector = self._connector()

        items = await connector.fetch()
        await connector.aclose()

        latest = next(i for i in items if i.external_id == "ARE:NY.GDP.MKTP.KD.ZG:2024")
        assert latest.summary is not None
        # 3.4 in 2024 against 2.7 in 2023.
        assert "3.4" in latest.summary
        assert "up from 2.7 in 2023" in latest.summary
        assert "+0.7" in latest.summary

    @respx.mock
    async def test_null_years_are_skipped(self) -> None:
        """A row with `"value": null` would become an item that asserts nothing."""
        respx.get(url__regex=r"https://api\.worldbank\.org/.*").mock(
            return_value=httpx.Response(200, json=WORLDBANK_PAYLOAD)
        )
        connector = self._connector()

        items = await connector.fetch()
        await connector.aclose()

        years = {item.raw["year"] for item in items}
        assert years == {2024, 2023, 2022}
        assert 2025 not in years
        assert 2026 not in years

    @respx.mock
    async def test_recent_years_trims_the_window(self) -> None:
        respx.get(url__regex=r"https://api\.worldbank\.org/.*").mock(
            return_value=httpx.Response(200, json=WORLDBANK_PAYLOAD)
        )
        connector = self._connector(recent_years=2)

        items = await connector.fetch()
        await connector.aclose()

        # The two most recent years that actually carry a figure — not the two most recent rows,
        # which are both null.
        assert {item.raw["year"] for item in items} == {2024, 2023}

    @respx.mock
    async def test_error_payload_does_not_crash_the_other_indicators(self) -> None:
        """`[{"message": ...}]` is a ONE-element list served with HTTP 200."""
        route = respx.get(url__regex=r"https://api\.worldbank\.org/.*")
        route.side_effect = [
            httpx.Response(200, json=WORLDBANK_ERROR_PAYLOAD),
            httpx.Response(200, json=WORLDBANK_PAYLOAD),
        ]
        connector = self._connector(indicators=["NOT.A.REAL.CODE", "NY.GDP.MKTP.KD.ZG"])

        items = await connector.fetch()
        await connector.aclose()

        # The good indicator's items survive the bad one.
        assert len(items) == 3

    @respx.mock
    async def test_every_indicator_failing_is_reported_as_an_error(self) -> None:
        """A source that returns nothing because it is broken must not look like a quiet success."""
        respx.get(url__regex=r"https://api\.worldbank\.org/.*").mock(
            return_value=httpx.Response(200, json=WORLDBANK_ERROR_PAYLOAD)
        )
        connector = self._connector(indicators=["NOT.A.REAL.CODE"])

        with pytest.raises(IngestionError, match="no usable data"):
            await connector.fetch()
        await connector.aclose()

    async def test_missing_config_is_rejected(self) -> None:
        connector = WorldBankConnector(SOURCE_ID, {"countries": ["ARE"]})

        with pytest.raises(IngestionError, match="config"):
            await connector.fetch()
        await connector.aclose()


# ---------------------------------------------------------------------------
# GDELT
# ---------------------------------------------------------------------------
class TestGdelt:
    def _connector(self, **overrides: Any) -> GdeltConnector:
        config: dict[str, Any] = {
            "query_terms": ["trade policy", "energy prices"],
            "max_records": 60,
            "timespan": "24h",
        }
        config.update(overrides)
        return GdeltConnector(SOURCE_ID, config)

    @respx.mock
    async def test_articles_become_items(self) -> None:
        route = respx.get(url__regex=r"https://api\.gdeltproject\.org/.*").mock(
            return_value=httpx.Response(200, json=GDELT_PAYLOAD)
        )
        connector = self._connector()

        items = await connector.fetch()
        await connector.aclose()

        assert len(items) == 2
        first = items[0]
        assert first.external_id == "https://news.example.com/uae-trade-policy"
        assert first.url == first.external_id
        assert first.published_at is not None
        assert first.published_at.hour == 10  # 20260713T104500Z
        assert first.classification is Classification.PUBLIC

        # Terms are quoted so "trade policy" stays a phrase rather than becoming an implicit AND.
        query = route.calls.last.request.url.params["query"]
        assert query == '"trade policy" OR "energy prices"'

    @respx.mock
    async def test_arabic_article_is_detected_as_arabic(self) -> None:
        respx.get(url__regex=r"https://api\.gdeltproject\.org/.*").mock(
            return_value=httpx.Response(200, json=GDELT_PAYLOAD)
        )
        connector = self._connector()

        items = await connector.fetch()
        await connector.aclose()

        assert items[0].language is Language.EN
        assert items[1].language is Language.AR

    @respx.mock
    async def test_non_json_200_raises_ingestion_error(self) -> None:
        """GDELT reports a rejected query as HTTP 200 with plain text — not as a 4xx."""
        respx.get(url__regex=r"https://api\.gdeltproject\.org/.*").mock(
            return_value=httpx.Response(200, text="Your query was too broad. Please narrow it.")
        )
        connector = self._connector()

        with pytest.raises(IngestionError, match="not JSON"):
            await connector.fetch()
        await connector.aclose()

    @respx.mock
    async def test_empty_200_body_raises_ingestion_error(self) -> None:
        respx.get(url__regex=r"https://api\.gdeltproject\.org/.*").mock(
            return_value=httpx.Response(200, text="")
        )
        connector = self._connector()

        with pytest.raises(IngestionError, match="empty body"):
            await connector.fetch()
        await connector.aclose()

    @respx.mock
    async def test_no_matching_coverage_is_not_an_error(self) -> None:
        """A quiet news window is a healthy answer, not a failure."""
        respx.get(url__regex=r"https://api\.gdeltproject\.org/.*").mock(
            return_value=httpx.Response(200, json={})
        )
        connector = self._connector()

        assert await connector.fetch() == []
        await connector.aclose()


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------
class TestRss:
    @respx.mock
    async def test_entries_become_items_with_html_stripped(self) -> None:
        respx.get(FEED_A).mock(return_value=httpx.Response(200, text=RSS_FEED_A))
        connector = RssConnector(SOURCE_ID, {"feeds": [FEED_A]})

        items = await connector.fetch()
        await connector.aclose()

        assert len(items) == 2
        first = items[0]
        assert first.title == "Supply chain disruption hits regional ports"
        assert first.url == "https://example.gov/articles/ports"
        # <guid> is the publisher's own id and survives a URL rewrite, so it wins over the link.
        assert first.external_id == "example-guid-001"
        assert first.published_at is not None
        assert first.published_at.day == 13

        assert first.summary is not None
        assert "fell 12%" in first.summary
        # Markup must not reach the embedder, the triage prompt or the UI.
        assert "<p>" not in first.summary
        assert "<b>" not in first.summary
        assert "href" not in first.summary

    @respx.mock
    async def test_entry_without_guid_falls_back_to_the_link(self) -> None:
        respx.get(FEED_A).mock(return_value=httpx.Response(200, text=RSS_FEED_A))
        connector = RssConnector(SOURCE_ID, {"feeds": [FEED_A]})

        items = await connector.fetch()
        await connector.aclose()

        assert items[1].external_id == "https://example.gov/articles/rates"

    @respx.mock
    async def test_one_failing_feed_does_not_cost_us_the_others(self) -> None:
        respx.get(FEED_A).mock(return_value=httpx.Response(404))
        respx.get(FEED_B).mock(return_value=httpx.Response(200, text=RSS_FEED_B))
        connector = RssConnector(SOURCE_ID, {"feeds": [FEED_A, FEED_B]})

        items = await connector.fetch()
        await connector.aclose()

        assert [item.title for item in items] == ["Gas prices climb on cold snap"]

    @respx.mock
    async def test_every_feed_failing_is_reported_as_an_error(self) -> None:
        respx.get(FEED_A).mock(return_value=httpx.Response(500))
        respx.get(FEED_B).mock(return_value=httpx.Response(500))
        connector = RssConnector(SOURCE_ID, {"feeds": [FEED_A, FEED_B]})

        with pytest.raises(IngestionError, match="Every configured RSS feed"):
            await connector.fetch()
        await connector.aclose()

    @respx.mock
    async def test_raw_holds_only_json_serialisable_primitives(self) -> None:
        """A feedparser entry carries `time.struct_time`, which JSONB cannot store."""
        respx.get(FEED_A).mock(return_value=httpx.Response(200, text=RSS_FEED_A))
        connector = RssConnector(SOURCE_ID, {"feeds": [FEED_A]})

        items = await connector.fetch()
        await connector.aclose()

        assert items[0].raw == {
            "feed_url": FEED_A,
            "feed_title": "Example Business News",
        }


# ---------------------------------------------------------------------------
# ReliefWeb
# ---------------------------------------------------------------------------
class TestReliefWeb:
    @respx.mock
    async def test_reports_become_items(self) -> None:
        respx.post(url__regex=r"https://api\.reliefweb\.int/.*").mock(
            return_value=httpx.Response(200, json=RELIEFWEB_PAYLOAD)
        )
        connector = ReliefWebConnector(SOURCE_ID, {"countries": ["are", "pak"], "limit": 40})

        items = await connector.fetch()
        await connector.aclose()

        assert len(items) == 2
        first = items[0]
        assert first.external_id == "4098765"
        assert first.title == "Pakistan: Monsoon Floods Situation Report No. 4"
        assert first.url == "https://reliefweb.int/report/pakistan/monsoon-4"
        assert first.published_at is not None
        assert first.published_at.day == 12
        assert first.raw["sources"] == ["OCHA"]
        assert first.raw["countries"] == ["Pakistan"]

        # The id arrives as a JSON number in some responses and a string in others.
        assert items[1].external_id == "4098766"

    @respx.mock
    async def test_country_filter_is_uppercased_iso3(self) -> None:
        route = respx.post(url__regex=r"https://api\.reliefweb\.int/.*").mock(
            return_value=httpx.Response(200, json=RELIEFWEB_PAYLOAD)
        )
        connector = ReliefWebConnector(SOURCE_ID, {"countries": ["are", "pak"], "limit": 40})

        await connector.fetch()
        await connector.aclose()

        body: dict[str, Any] = json.loads(route.calls.last.request.content)
        assert body["filter"] == {"field": "country.iso3", "value": ["ARE", "PAK"]}
        assert body["limit"] == 40
        assert body["preset"] == "latest"

    @respx.mock
    async def test_missing_data_array_is_an_error(self) -> None:
        respx.post(url__regex=r"https://api\.reliefweb\.int/.*").mock(
            return_value=httpx.Response(200, json={"error": {"message": "bad request"}})
        )
        connector = ReliefWebConnector(SOURCE_ID, {"countries": ["are"]})

        with pytest.raises(IngestionError, match="no 'data' array"):
            await connector.fetch()
        await connector.aclose()


# ---------------------------------------------------------------------------
# Dedup identity — the property that keeps a re-poll from duplicating the corpus
# ---------------------------------------------------------------------------
class TestDedupHash:
    def test_same_item_same_source_is_the_same_hash(self) -> None:
        a = RawItem(title="Trade policy shift", external_id="abc-123")
        b = RawItem(title="Trade policy shift", external_id="abc-123")

        assert a.dedup_hash(SOURCE_ID) == b.dedup_hash(SOURCE_ID)

    def test_the_same_item_from_a_different_source_is_a_different_item(self) -> None:
        """Two sources reporting the same story are two observations, and are counted as two."""
        item = RawItem(title="Trade policy shift", external_id="abc-123")

        assert item.dedup_hash(SOURCE_ID) != item.dedup_hash(OTHER_SOURCE_ID)

    def test_title_only_items_on_different_dates_are_different_items(self) -> None:
        """A "Weekly Economic Update" is a new item every week, not one item re-served forever."""
        monday = RawItem(
            title="Weekly Economic Update", published_at=datetime(2026, 7, 6, tzinfo=UTC)
        )
        next_monday = RawItem(
            title="Weekly Economic Update", published_at=datetime(2026, 7, 13, tzinfo=UTC)
        )

        assert monday.dedup_hash(SOURCE_ID) != next_monday.dedup_hash(SOURCE_ID)

    def test_title_only_items_on_the_same_date_collapse(self) -> None:
        published = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
        a = RawItem(title="Weekly Economic Update", published_at=published)
        # A re-poll a few hours later: same headline, same day, different clock reading.
        b = RawItem(
            title="  weekly economic update  ",
            published_at=datetime(2026, 7, 13, 19, 30, tzinfo=UTC),
        )

        assert a.dedup_hash(SOURCE_ID) == b.dedup_hash(SOURCE_ID)

    def test_external_id_wins_over_url(self) -> None:
        """A publisher rewriting its URLs must not re-create every item it has ever published."""
        before = RawItem(title="T", external_id="id-1", url="https://a.example/old")
        after = RawItem(title="T", external_id="id-1", url="https://a.example/new")

        assert before.dedup_hash(SOURCE_ID) == after.dedup_hash(SOURCE_ID)


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------
class TestWebhookSignature:
    SECRET = "shared-secret-from-the-licensed-feed"
    BODY = b'{"items":[{"title":"Reuters: OPEC+ extends cuts"}]}'

    def _signature(self, body: bytes = BODY, secret: str = SECRET) -> str:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return f"{SIGNATURE_PREFIX}{digest}"

    def test_a_correct_signature_verifies(self) -> None:
        assert verify_signature(self.BODY, self._signature(), self.SECRET) is True

    def test_uppercase_hex_verifies(self) -> None:
        signature = self._signature().upper().replace("SHA256=", SIGNATURE_PREFIX)

        assert verify_signature(self.BODY, signature, self.SECRET) is True

    def test_a_wrong_signature_does_not_verify(self) -> None:
        forged = f"{SIGNATURE_PREFIX}{'0' * 64}"

        assert verify_signature(self.BODY, forged, self.SECRET) is False

    def test_a_signature_for_a_different_body_does_not_verify(self) -> None:
        """The digest must cover the bytes as received — this is the replay/tamper guard."""
        signature = self._signature(body=b'{"items":[]}')

        assert verify_signature(self.BODY, signature, self.SECRET) is False

    def test_a_signature_from_a_different_secret_does_not_verify(self) -> None:
        signature = self._signature(secret="not-the-shared-secret")

        assert verify_signature(self.BODY, signature, self.SECRET) is False

    @pytest.mark.parametrize(
        "header",
        [
            "",
            "deadbeef",  # no algorithm prefix
            "sha256=",  # prefix, no digest
            "sha1=deadbeef",  # wrong algorithm
            "sha256=zzzz",  # not hex
            "sha256=صحيح",  # non-ASCII: compare_digest would raise TypeError on this
        ],
    )
    def test_a_malformed_header_is_false_not_an_exception(self, header: str) -> None:
        assert verify_signature(self.BODY, header, self.SECRET) is False

    def test_an_unconfigured_secret_never_verifies(self) -> None:
        """An empty secret must fail closed, not accept everything."""
        assert verify_signature(self.BODY, self._signature(), "") is False
