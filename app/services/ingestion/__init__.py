"""Source connectors, the sync runner, and HMAC-verified webhook ingestion.

Every connector reads its parameters from the owning `sources.config` JSONB blob rather than
from module constants, so an operator can retarget a source (new countries, new indicators, new
feeds) from the admin API without a deploy. That blob is operator-supplied JSON, which means it
can be the wrong *shape* as easily as the wrong *value* — a string where a list belongs, a
stringified integer from a form post. The coercion helpers below are the single place that turns
untrusted JSON into the typed values a connector can rely on, so no connector has to re-derive
"what if this key is missing / null / the wrong type" for itself.
"""

from __future__ import annotations

from typing import Any

__all__ = ["config_int", "config_str", "config_str_list"]


def config_str_list(config: dict[str, Any], key: str) -> list[str]:
    """Read `key` as a list of non-empty strings. A bare string is accepted as a 1-element list."""
    value = config.get(key)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def config_int(config: dict[str, Any], key: str, default: int, *, minimum: int = 1) -> int:
    """Read `key` as an int, clamped to at least `minimum`.

    A non-numeric or absent value falls back to `default` rather than raising: a typo in one
    tuning knob should not take a whole source offline.
    """
    value = config.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, minimum)


def config_str(config: dict[str, Any], key: str, default: str) -> str:
    value = config.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default
