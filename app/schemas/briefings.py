"""Bilingual briefing schemas (§7.7 #17–18)."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from datetime import datetime

from pydantic import Field

from app.enums import Classification, PublicationStatus
from app.schemas.common import Citation, DanahModel


class BriefingSection(DanahModel):
    key: str = Field(
        description="'exec_summary' | 'top_risks' | 'top_opportunities' | 'policy_watch' | 'decisions'"
    )
    heading_en: str
    heading_ar: str
    body_en: str
    body_ar: str


class BriefingOut(DanahModel):
    id: uuid.UUID
    # `date` is both the field name (the wire contract) and the field type. Inside a class body
    # the name binds to the field, so annotating it as plain `date` makes Pydantic resolve the
    # *field* — on `BriefingGenerateRequest` below, whose `date` holds a `Field(...)` default,
    # that evaluates `FieldInfo | None` and the module cannot even be imported. The type is
    # therefore always referred to by an alias in this file.
    date: date_type
    title: str
    confidence: float = Field(ge=0.0, le=1.0)
    classification: Classification
    status: PublicationStatus
    run_id: uuid.UUID | None = None
    created_at: datetime


class BriefingDetail(BriefingOut):
    body_en: str
    body_ar: str = Field(description="Faithful Arabic rendering; never omitted (master prompt §12)")
    sections: list[BriefingSection] = []
    citations: list[Citation] = []
    approval_id: uuid.UUID | None = None
    approval_status: str | None = None


class BriefingGenerateRequest(DanahModel):
    date: date_type | None = Field(default=None, description="Defaults to today (server TZ)")
    force: bool = Field(
        default=False,
        description="Regenerate even if a briefing already exists for that date",
    )
