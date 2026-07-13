"""World Bank Indicators API v2 — macro-economic series for the watched countries.

No API key. One request per indicator, batched across every configured country
(`/country/ARE;SAU;PAK/indicator/NY.GDP.MKTP.KD.ZG`).

Two properties of this API drive the shape of the code:

* The response is `[metadata, [rows]]` — a **two-element** list. On a bad indicator code it is
  instead a *one*-element list `[{"message": [...]}]`, with HTTP 200. Indexing `[1]` blindly is
  therefore an IndexError waiting for the first typo in an operator's config.
* Rows exist for every year in the requested window whether or not the World Bank holds a figure;
  the missing ones come back as `"value": null`. Ingesting those would create items that assert
  nothing ("UAE — GDP growth: None in 2026") and would still cost a Signal Agent triage each.

So: nulls are dropped, and the most recent `recent_years` *observations* are kept. One extra year
is requested beyond that window purely so the oldest kept year still has a prior year available
for its year-on-year comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import structlog

from app.enums import Classification, ConnectorKind, Language
from app.exceptions import IngestionError
from app.services.ingestion import config_int, config_str, config_str_list
from app.services.ingestion.base_connector import BaseConnector, RawItem

log = structlog.get_logger(__name__)

# The published, keyless endpoint. Overridable per source (`config["base_url"]`) so a mirror or a
# staging endpoint never requires a code change.
WORLDBANK_API_BASE: Final = "https://api.worldbank.org/v2"
# Public landing page for a series — gives the analyst somewhere to click through to.
WORLDBANK_SERIES_URL: Final = "https://data.worldbank.org/indicator"

DEFAULT_RECENT_YEARS: Final = 5
# One year of headroom beyond the kept window, so the oldest kept observation still has a
# predecessor to compute a year-on-year change against.
YOY_LOOKBACK_YEARS: Final = 1


@dataclass(slots=True)
class _Observation:
    """One (country, indicator, year) datapoint that carries an actual figure."""

    country_iso: str
    country_name: str
    indicator_code: str
    indicator_name: str
    year: int
    value: float


@dataclass(slots=True)
class _Series:
    """The observations of a single country × indicator, indexed by year."""

    by_year: dict[int, _Observation] = field(default_factory=dict)

    def to_items(self, recent_years: int) -> list[RawItem]:
        # Most recent first, then trimmed. The window is over the years that actually *have* a
        # figure, so a null-heavy indicator still yields `recent_years` real observations.
        kept = sorted(self.by_year, reverse=True)[:recent_years]
        return [_to_item(self.by_year[year], self.by_year.get(year - 1)) for year in kept]


class WorldBankConnector(BaseConnector):
    """Annual indicator observations, one `RawItem` per country × indicator × year."""

    kind = ConnectorKind.WORLDBANK

    async def fetch(self, since: datetime | None = None) -> list[RawItem]:
        """Pull the recent window for every configured indicator.

        `since` is deliberately unused: World Bank series are annual and are revised in place, so
        there is no "new since" cursor to follow. A re-poll costs one request per indicator, and
        every row it returns carries a stable `external_id`, so the runner's dedup absorbs the
        repeat rather than duplicating the corpus.
        """
        countries = [c.upper() for c in config_str_list(self.config, "countries")]
        indicators = config_str_list(self.config, "indicators")
        if not countries or not indicators:
            raise IngestionError(
                "World Bank source is missing 'countries' or 'indicators' in its config.",
                code="source_misconfigured",
                detail={"source_id": str(self.source_id)},
            )

        recent_years = config_int(self.config, "recent_years", DEFAULT_RECENT_YEARS)
        window = recent_years + YOY_LOOKBACK_YEARS

        items: list[RawItem] = []
        failed: list[str] = []
        for indicator in indicators:
            rows = await self._fetch_indicator(indicator, countries, window)
            if rows is None:
                failed.append(indicator)
                continue
            items.extend(_items_from_rows(rows, recent_years=recent_years))

        # Every indicator failing means the source itself is broken (renamed codes, an API
        # change) rather than the world simply having had no news. Say so, instead of reporting
        # a healthy "0 new" and letting a dead source look idle.
        if failed and not items:
            raise IngestionError(
                "The World Bank API returned no usable data for any configured indicator.",
                detail={"indicators": failed, "source_id": str(self.source_id)},
            )

        log.info(
            "worldbank_fetched",
            source_id=str(self.source_id),
            indicators=len(indicators),
            countries=len(countries),
            items=len(items),
            failed_indicators=len(failed),
        )
        return items

    async def _fetch_indicator(
        self, indicator: str, countries: list[str], window: int
    ) -> list[Any] | None:
        """Return the datapoint rows, or None when the API answered with an error document."""
        base = config_str(self.config, "base_url", WORLDBANK_API_BASE).rstrip("/")
        url = f"{base}/country/{';'.join(countries)}/indicator/{indicator}"
        payload = await self.get_json(
            url,
            params={
                "format": "json",
                # `mrv` = most recent values, per country. Bounding the window server-side keeps
                # the response to roughly the rows we intend to keep.
                "mrv": window,
                "per_page": len(countries) * window,
            },
        )

        # `[{"message": [...]}]` — a *one*-element list — is how this API reports a bad indicator
        # code, with HTTP 200. A null second element is how it reports "no rows".
        if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
            log.warning(
                "worldbank_indicator_unavailable",
                source_id=str(self.source_id),
                indicator=indicator,
            )
            return None
        return payload[1]


def _items_from_rows(rows: list[Any], *, recent_years: int) -> list[RawItem]:
    # Group before emitting: the year-on-year figure in an item's summary needs the neighbouring
    # row of the same series, and the API interleaves the series across countries.
    series: dict[tuple[str, str], _Series] = {}
    for row in rows:
        observation = _parse_row(row)
        if observation is None:
            continue
        key = (observation.country_iso, observation.indicator_code)
        series.setdefault(key, _Series()).by_year[observation.year] = observation

    items: list[RawItem] = []
    for entry in series.values():
        items.extend(entry.to_items(recent_years))
    return items


def _parse_row(row: Any) -> _Observation | None:
    """Coerce one API row, or None if it is unusable (null figure, malformed year)."""
    if not isinstance(row, dict):
        return None

    value = row.get("value")
    # The year the World Bank holds no figure for. An item built from it would assert nothing.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None

    year_raw = str(row.get("date", "")).strip()
    if not year_raw.isdigit():
        return None

    raw_country = row.get("country")
    raw_indicator = row.get("indicator")
    country: dict[str, Any] = raw_country if isinstance(raw_country, dict) else {}
    indicator: dict[str, Any] = raw_indicator if isinstance(raw_indicator, dict) else {}

    country_name = str(country.get("value") or "").strip()
    indicator_name = str(indicator.get("value") or "").strip()
    indicator_code = str(indicator.get("id") or "").strip()
    # `countryiso3code` is the stable identifier. `country.id` (ISO2) is the fallback for the
    # aggregate rows — regions, income groups — which carry no ISO3 code.
    country_iso = str(row.get("countryiso3code") or country.get("id") or "").strip().upper()

    if not (country_name and indicator_name and indicator_code and country_iso):
        return None

    return _Observation(
        country_iso=country_iso,
        country_name=country_name,
        indicator_code=indicator_code,
        indicator_name=indicator_name,
        year=int(year_raw),
        value=float(value),
    )


def _to_item(current: _Observation, prior: _Observation | None) -> RawItem:
    figure = _format_figure(current.value)
    title = f"{current.country_name} — {current.indicator_name}: {figure} in {current.year}"

    change: float | None = None
    if prior is None:
        summary = (
            f"{current.indicator_name} for {current.country_name} stands at {figure} in "
            f"{current.year}. The API returned no figure for the preceding year, so no "
            f"year-on-year change can be stated."
        )
    else:
        change = current.value - prior.value
        direction = "unchanged from" if change == 0 else ("up from" if change > 0 else "down from")
        summary = (
            f"{current.indicator_name} for {current.country_name} stands at {figure} in "
            f"{current.year}, {direction} {_format_figure(prior.value)} in {prior.year} "
            f"({_format_figure(change, signed=True)} year-on-year)."
        )

    return RawItem(
        title=title,
        # Stable across re-polls: the same observation always hashes to the same item, even after
        # the World Bank revises the figure.
        external_id=f"{current.country_iso}:{current.indicator_code}:{current.year}",
        summary=summary,
        url=f"{WORLDBANK_SERIES_URL}/{current.indicator_code}?locations={current.country_iso}",
        # The observation describes a calendar year; date it to the end of that period rather than
        # to the moment we happened to poll.
        published_at=datetime(current.year, 12, 31, tzinfo=UTC),
        language=Language.EN,
        classification=Classification.PUBLIC,
        raw={
            "country": current.country_name,
            "country_iso3": current.country_iso,
            "indicator_code": current.indicator_code,
            "indicator_name": current.indicator_name,
            "year": current.year,
            "value": current.value,
            "previous_value": None if prior is None else prior.value,
            "change": change,
        },
    )


def _format_figure(value: float, *, signed: bool = False) -> str:
    """Two decimal places at most, with the noise of trailing zeros removed."""
    text = f"{value:+,.2f}" if signed else f"{value:,.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


__all__ = ["WorldBankConnector"]
