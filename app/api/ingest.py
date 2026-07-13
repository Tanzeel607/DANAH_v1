"""Webhook ingestion (§7.7 #24). Mounted at /api/ingest.

**This route carries no JWT.** It is machine-to-machine: the producer proves itself by signing the
body with the source's HMAC secret. This is the seam a licensed feed (Bloomberg, Reuters) arrives
through later — it lands in the same `ingested_items` table, deduplicated by the same rule, and
flows into the same pipeline, with no new code path (architecture §9).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import client_ip, get_config, get_db
from app.enums import ActorType
from app.exceptions import AuthError, ConflictError, InvalidRequestError
from app.models import Source
from app.schemas.sources import WebhookResponse
from app.services.audit_service import record_audit
from app.services.ingestion.webhook import ingest_webhook_payload, verify_signature

log = structlog.get_logger(__name__)

router = APIRouter(tags=["ingest"])

SIGNATURE_HEADER: Final = "X-DANAH-Signature"


def _items(payload: Any) -> list[dict[str, Any]]:
    """Accept `{"items": [...]}` or a bare `[...]` — producers do both, and neither is worth a
    negotiation with a vendor."""
    batch = payload.get("items") if isinstance(payload, dict) else payload

    if not isinstance(batch, list):
        raise InvalidRequestError(
            "Expected a JSON array of items, or an object with an 'items' array.",
            code="invalid_payload",
        )
    if not all(isinstance(item, dict) for item in batch):
        raise InvalidRequestError(
            "Every item must be a JSON object.",
            code="invalid_payload",
        )
    return list(batch)


@router.post(
    "/webhook/{source_id}",
    response_model=WebhookResponse,
    summary="Push items into a source (HMAC-signed; no JWT)",
    description=(
        "Sign the **raw request body** with the source's HMAC secret and send it as "
        "`X-DANAH-Signature: sha256=<hex>`. The signature is compared in constant time; an "
        "invalid or missing one is a `401`.\n\n"
        'Body: a JSON array of items, or `{"items": [...]}`. Each item takes `title` '
        "(required), and optionally `external_id`, `summary`, `content`, `url`, `published_at`, "
        "`language` and `classification`.\n\n"
        "Items deduplicate on `dedup_hash`, so re-sending a batch after a network timeout cannot "
        "duplicate the corpus. A payload may raise an item's classification above the source's "
        "configured floor, but never lower it."
    ),
    responses={401: {"description": "Missing or invalid signature, or unknown source."}},
)
async def receive_webhook(
    source_id: uuid.UUID,
    request: Request,
    signature: str = Header(default="", alias=SIGNATURE_HEADER),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_config),
) -> WebhookResponse:
    # The raw bytes, read before anything parses them: a signature covers what was *sent*, and
    # re-serialising the parsed JSON would reorder keys and change the digest.
    body = await request.body()

    source = await db.get(Source, source_id)

    # An unknown source and a bad signature are answered identically, and cost the same HMAC
    # computation: distinguishing them — by status code or by response time — would let an
    # unauthenticated caller enumerate which source ids exist.
    signed = verify_signature(body, signature, _secret_for(source, settings))
    if source is None or not signed:
        log.warning(
            "webhook_rejected",
            source_id=str(source_id),
            reason="unknown_source" if source is None else "bad_signature",
            ip=client_ip(request),
            body_bytes=len(body),
        )
        raise AuthError("Signature verification failed.", code="invalid_signature")

    if not source.enabled:
        raise ConflictError(
            "This source is disabled and is not accepting pushes.",
            detail={"source_id": str(source_id)},
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InvalidRequestError(
            "The request body is not valid JSON.", code="invalid_payload"
        ) from exc

    items = _items(payload)
    accepted, duplicates = await ingest_webhook_payload(db, source, items)

    await record_audit(
        db,
        action="ingest.webhook",
        # The producer is a machine holding a shared secret, not a DANAH user — there is no
        # actor_id to record, and inventing one would be a lie in the accountability trail.
        actor_type=ActorType.SYSTEM,
        subject_type="source",
        subject_id=source.id,
        ip=client_ip(request),
        detail={"received": len(items), "accepted": accepted, "duplicates": duplicates},
    )

    log.info(
        "webhook_accepted",
        source_id=str(source.id),
        # Counts only — a licensed feed's content may be OFFICIAL-SENSITIVE.
        received=len(items),
        accepted=accepted,
        duplicates=duplicates,
    )

    return WebhookResponse(accepted=accepted, duplicates=duplicates, source_id=source.id)


def _secret_for(source: Source | None, settings: Settings) -> str:
    """The source's own secret, falling back to the shared default.

    Resolved even when the source does not exist, so that the unknown-source path costs the same
    HMAC computation as the bad-signature path and cannot be told apart by timing.
    """
    if source is not None and source.hmac_secret:
        return source.hmac_secret
    return settings.webhook_hmac_default_secret.get_secret_value()
