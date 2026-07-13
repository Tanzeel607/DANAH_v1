"""HMAC-verified push ingestion — the door a licensed feed comes in through.

This is why a Bloomberg or Reuters contract signed a year from now needs no schema change and no
new code path: the producer signs a JSON batch, we verify it, normalise it to `RawItem` and hand
it to the *same* `persist_items` the pollers use. Same dedup rule, same table, same downstream
pipeline.

Two security properties matter here:

* **The signature is compared in constant time.** `signature == expected` leaks, through its own
  timing, how many leading bytes of the digest were correct — enough to forge a signature byte by
  byte over enough attempts. `hmac.compare_digest` does not.
* **A producer cannot *downgrade* a classification.** The source's configured classification is a
  floor set by the operator, not a default the payload can talk its way underneath. A buggy or
  compromised producer stamping `PUBLIC` on an OFFICIAL feed would otherwise drop that content
  into every viewer's clearance — and clearance is enforced as a SQL WHERE clause, which would
  then faithfully hand it out.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import CLASSIFICATION_RANK, Classification, Language
from app.exceptions import InvalidRequestError
from app.models import Source
from app.services.ingestion.base_connector import BaseConnector, RawItem
from app.services.ingestion.runner import persist_items

log = structlog.get_logger(__name__)

SIGNATURE_PREFIX: Final = "sha256="

# The keys a producer may use for the publication timestamp, in precedence order. Licensed feeds
# each have their own house style and none of them will change it for us.
PUBLISHED_KEYS: Final = ("published_at", "published", "date")


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify an `X-Signature: sha256=<hex>` header against the raw request body.

    `body` must be the bytes as received: re-serialising the parsed JSON would reorder keys and
    change the digest. Any malformed input — wrong prefix, empty hex, non-ASCII — is a quiet
    `False`, never an exception, so a hostile header cannot turn into a 500.
    """
    if not signature or not secret:
        return False
    if not signature.startswith(SIGNATURE_PREFIX):
        return False

    provided = signature[len(SIGNATURE_PREFIX) :].strip().lower()
    # `compare_digest` raises TypeError on a non-ASCII str, so screen that out first.
    if not provided or not provided.isascii():
        return False

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


async def ingest_webhook_payload(
    session: AsyncSession, source: Source, items: list[dict[str, Any]]
) -> tuple[int, int]:
    """Normalise a verified push batch and persist it. Returns (accepted, duplicates).

    Reuses `runner.persist_items`, so pushed items deduplicate on the same `dedup_hash` as polled
    ones — a producer re-sending a batch after a network timeout cannot duplicate the corpus.
    """
    floor = _configured_classification(source)
    raw_items = [_to_raw_item(item, floor, index) for index, item in enumerate(items)]

    created, duplicates = await persist_items(session, source, raw_items)

    # A webhook source is only ever "alive" when a producer pushes to it, so a delivery *is* its
    # sync. Without this it would sit at `last_synced_at = null` and report as never-polled for
    # the rest of its life.
    source.last_synced_at = datetime.now(UTC)
    source.last_status = f"ok: {created} new"
    await session.flush()

    log.info(
        "webhook_ingested",
        source_id=str(source.id),
        # Counts only — a licensed feed's content may be OFFICIAL-SENSITIVE.
        received=len(items),
        created=created,
        duplicates=duplicates,
    )
    return created, duplicates


def _to_raw_item(payload: dict[str, Any], floor: Classification, index: int) -> RawItem:
    title = _as_str(payload.get("title"))
    if not title:
        # Nothing to show, triage or embed. Reject the batch rather than silently dropping the
        # item, so the producer finds out it is sending us garbage.
        raise InvalidRequestError(
            "Each item requires a non-empty 'title'.", detail={"index": index}
        )

    url = _as_str(payload.get("url"))
    return RawItem(
        title=title,
        # The producer's own id is the only identity stable across a re-send; the URL is the
        # fallback, and `dedup_hash` falls back again to title+date if neither is present.
        external_id=_as_str(payload.get("external_id")) or _as_str(payload.get("id")) or url,
        summary=_as_str(payload.get("summary")),
        content=_as_str(payload.get("content")),
        url=url,
        published_at=_published_at(payload),
        language=_language(payload.get("language")),
        classification=_classification(payload.get("classification"), floor),
        raw=payload,
    )


def _configured_classification(source: Source) -> Classification:
    """The operator-set floor for this source — e.g. a licensed feed configured as OFFICIAL."""
    configured = source.config.get("classification")
    if isinstance(configured, str):
        try:
            return Classification(configured.upper())
        except ValueError:
            log.warning(
                "webhook_source_bad_classification",
                source_id=str(source.id),
                configured=configured,
            )
    return Classification.PUBLIC


def _classification(value: Any, floor: Classification) -> Classification:
    """A payload may raise the classification above the source's floor. It may never lower it."""
    if not isinstance(value, str):
        return floor
    try:
        declared = Classification(value.upper())
    except ValueError:
        return floor
    return declared if CLASSIFICATION_RANK[declared] > CLASSIFICATION_RANK[floor] else floor


def _language(value: Any) -> Language:
    if isinstance(value, str):
        try:
            return Language(value.strip().lower())
        except ValueError:
            return Language.EN
    return Language.EN


def _published_at(payload: dict[str, Any]) -> datetime | None:
    # The connectors' parser, reused: a pushed timestamp is read exactly as a polled one is.
    for key in PUBLISHED_KEYS:
        parsed = BaseConnector.parse_datetime(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


__all__ = ["ingest_webhook_payload", "verify_signature"]
