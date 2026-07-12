"""Document blob storage.

Local filesystem today; the S3 branch is wired through the same interface so moving to a
sovereign S3-compatible endpoint (MinIO) is an env change, not a code change.

Path safety matters here: `storage_path` reaches this module from a database row, and a row
could in principle carry `../../etc/passwd`. Every path is resolved and asserted to live under
the configured root before it is opened.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog

from app.config import Settings, get_settings
from app.enums import StorageBackend
from app.exceptions import DanahError, RetrievalError

log = structlog.get_logger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class StorageError(DanahError):
    code = "storage_error"
    message = "The document could not be stored or retrieved."


def _root(settings: Settings) -> Path:
    root = Path(settings.storage_local_path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_filename(filename: str) -> str:
    """Reduce a user-supplied filename to a safe basename, preserving the extension."""
    name = Path(filename).name  # discards any directory component
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("._")
    return cleaned or "document"


def _resolve_within_root(storage_path: str, settings: Settings) -> Path:
    root = _root(settings)
    candidate = (
        (root / storage_path).resolve()
        if not Path(storage_path).is_absolute()
        else Path(storage_path).resolve()
    )

    # Path traversal guard: the resolved path must stay under the storage root.
    if not candidate.is_relative_to(root):
        raise StorageError(
            "Refusing to read outside the document storage root.",
            detail={"storage_path": storage_path},
        )
    return candidate


async def write_document(
    data: bytes,
    *,
    filename: str,
    document_id: uuid.UUID,
    settings: Settings | None = None,
) -> str:
    """Persist the original upload and return the `storage_path` to record on the row.

    The stored name is `<date>/<uuid>-<safe-name>`: the uuid makes collisions impossible, the
    date prefix keeps directories from growing without bound, and the original name is retained
    so an operator browsing the volume can tell what a file is.
    """
    cfg = settings or get_settings()

    if cfg.storage_backend is StorageBackend.S3:
        return await _write_s3(data, filename=filename, document_id=document_id, settings=cfg)

    folder = datetime.now(UTC).strftime("%Y-%m")
    relative = Path(folder) / f"{document_id}-{safe_filename(filename)}"
    target = _root(cfg) / relative
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        target.write_bytes(data)
    except OSError as exc:
        raise StorageError(
            "Could not write the uploaded document.", detail={"error": str(exc)}
        ) from exc

    return relative.as_posix()


async def read_document(storage_path: str, settings: Settings | None = None) -> bytes:
    cfg = settings or get_settings()

    if cfg.storage_backend is StorageBackend.S3:
        return await _read_s3(storage_path, cfg)

    path = _resolve_within_root(storage_path, cfg)
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise RetrievalError(
            "The stored document file is missing.",
            code="document_file_missing",
            detail={"storage_path": storage_path},
        ) from exc
    except OSError as exc:
        raise StorageError(
            "Could not read the stored document.", detail={"error": str(exc)}
        ) from exc


async def delete_document(storage_path: str, settings: Settings | None = None) -> None:
    cfg = settings or get_settings()
    if cfg.storage_backend is StorageBackend.S3:
        return

    path = _resolve_within_root(storage_path, cfg)
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# S3-compatible backend (sovereign MinIO or equivalent).
# ---------------------------------------------------------------------------
async def _write_s3(
    data: bytes,
    *,
    filename: str,
    document_id: uuid.UUID,
    settings: Settings,
) -> str:
    raise StorageError(
        "STORAGE_BACKEND=s3 is configured but the S3 client is not enabled in this build. "
        "Set STORAGE_BACKEND=local, or add an S3 client (boto3/aioboto3) — the interface here "
        "is the only place that needs to change.",
        detail={"bucket": settings.s3_bucket, "filename": filename, "id": str(document_id)},
    )


async def _read_s3(storage_path: str, settings: Settings) -> bytes:
    raise StorageError(
        "STORAGE_BACKEND=s3 is configured but the S3 client is not enabled in this build.",
        detail={"bucket": settings.s3_bucket, "storage_path": storage_path},
    )
