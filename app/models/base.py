"""Shared column helpers for the model layer."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from app.config import get_settings


def pg_enum(enum_cls: type[StrEnum], name: str) -> sa.Enum:
    """A native Postgres enum that stores the enum *values*, not the member names.

    SQLAlchemy's default is to persist `Role.ADMIN` as `"ADMIN"`. Our wire contract is the
    value (`"admin"`), so `values_callable` is mandatory — without it the API and the DB
    would disagree about every enum.
    """
    return sa.Enum(
        enum_cls,
        name=name,
        values_callable=lambda e: [member.value for member in e],
        native_enum=True,
        validate_strings=True,
    )


def embedding_column(**kwargs: Any) -> sa.Column[Any]:
    """A `vector(EMBEDDING_DIM)` column sized from config."""
    return sa.Column(Vector(get_settings().embedding_dim), **kwargs)


def embedding_dim() -> int:
    return get_settings().embedding_dim
