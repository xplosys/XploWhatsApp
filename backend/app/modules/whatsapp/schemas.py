from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SignupConfigOut(BaseModel):
    enabled: bool
    app_id: str | None
    config_id: str | None
    graph_api_version: str
    embedded_signup_version: str
    missing: list[str]


class OnboardingSessionOut(BaseModel):
    session_id: str
    expires_in: int


class OnboardingComplete(BaseModel):
    session_id: str = Field(min_length=16, max_length=128)
    code: str = Field(min_length=1, max_length=2048)
    waba_id: str = Field(pattern=r"^\d{5,32}$")
    phone_number_id: str | None = Field(default=None, pattern=r"^\d{5,32}$")
    business_id: str | None = Field(default=None, pattern=r"^\d{1,32}$")
    event: str | None = Field(default=None, max_length=64)


class NumberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    phone_number_id: str
    display_phone_number: str | None
    verified_name: str | None
    quality_rating: str | None
    messaging_limit_tier: str | None
    name_status: str | None
    meta_status: str | None
    status: str
    is_coexistence: bool
    registered_at: datetime | None


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    waba_id: str
    meta_business_id: str | None
    name: str | None
    currency: str | None
    status: str
    health: str
    health_issues: list[dict[str, Any]]
    last_health_check_at: datetime | None
    connected_at: datetime | None
    disconnected_at: datetime | None
    templates_synced_at: datetime | None
    numbers: list[NumberOut] = []


class DisconnectOut(BaseModel):
    account: AccountOut
    meta_notified: bool


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    name: str
    language: str
    category: str | None
    status: str
    rejected_reason: str | None
    components: list[dict[str, Any]]
    last_synced_at: datetime | None


class TemplateSyncOut(BaseModel):
    synced: int


class PinOut(BaseModel):
    pin: str
