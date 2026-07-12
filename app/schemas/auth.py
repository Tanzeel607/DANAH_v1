"""Auth request/response schemas (§7.7 #1–3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import EmailStr, Field

from app.enums import Classification, Role
from app.schemas.common import DanahModel


class LoginRequest(DanahModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=256)]


class RefreshRequest(DanahModel):
    refresh_token: Annotated[str, Field(min_length=10, max_length=4096)]


class TokenPair(DanahModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access-token lifetime in seconds")


class UserOut(DanahModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: Role
    clearance: Classification = Field(
        description="Highest classification this user may read; derived from role"
    )
    is_active: bool
    last_login_at: datetime | None = None
    created_at: datetime


class UserCreate(DanahModel):
    email: EmailStr
    full_name: Annotated[str, Field(min_length=1, max_length=200)]
    password: Annotated[str, Field(min_length=12, max_length=256)]
    role: Role = Role.VIEWER


class UserUpdate(DanahModel):
    full_name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    role: Role | None = None
    is_active: bool | None = None
    password: Annotated[str, Field(min_length=12, max_length=256)] | None = None
