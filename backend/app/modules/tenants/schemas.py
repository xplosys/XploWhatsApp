from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints

from app.modules.tenants.models import TenantRole

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: str
    country: str
    timezone: str
    default_language: str
    industry: str | None
    settings: dict[str, Any]
    created_at: datetime


class TenantUpdate(BaseModel):
    name: Name | None = None
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None
    timezone: Annotated[str, StringConstraints(max_length=64)] | None = None
    default_language: Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")] | None = None
    industry: Annotated[str, StringConstraints(max_length=64)] | None = None


class MemberOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    full_name: str
    role: TenantRole
    status: str
    created_at: datetime


class MemberUpdate(BaseModel):
    role: TenantRole | None = None
    status: Annotated[str, StringConstraints(pattern=r"^(active|suspended)$")] | None = None


class InvitationCreate(BaseModel):
    email: EmailStr
    role: TenantRole


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: TenantRole
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
