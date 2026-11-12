from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.modules.ai.assistant import AssistantConfig
from app.modules.ai.models import KeySource, ProviderKey


class ProviderOut(BaseModel):
    provider: ProviderKey
    label: str
    enabled: bool
    key_source: KeySource
    has_own_key: bool
    key_hint: str | None
    platform_key_available: bool
    ready: bool
    last_test_at: datetime | None
    last_test_ok: bool | None
    last_error: str | None


class ProviderUpdate(BaseModel):
    enabled: bool
    key_source: KeySource = KeySource.TENANT
    # Write-only. Omit to keep the stored key.
    api_key: SecretStr | None = Field(default=None, min_length=8, max_length=500)


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    model_id: str
    display_name: str | None
    context_window: int | None
    max_output_tokens: int | None


class VersionOut(BaseModel):
    version: int
    config: dict[str, Any]
    created_at: datetime
    created_by_user_id: uuid.UUID | None


class AssistantOut(BaseModel):
    id: uuid.UUID
    name: str
    auto_reply_enabled: bool
    active_version: VersionOut | None
    warnings: list[str]


class AssistantSave(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    config: AssistantConfig


class AssistantToggle(BaseModel):
    auto_reply_enabled: bool


class PreviewTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=4096)


class PreviewIn(BaseModel):
    messages: list[PreviewTurn] = Field(min_length=1, max_length=20)
    # Try unsaved settings; omit to use the active version.
    config: AssistantConfig | None = None


class AttemptOut(BaseModel):
    provider: str
    model: str
    outcome: str
    detail: str | None


class PreviewOut(BaseModel):
    kind: Literal["reply", "handover"]
    text: str
    reason: str
    provider: str | None
    model: str | None
    out_of_scope: bool
    attempts: list[AttemptOut]


class UsageRow(BaseModel):
    provider: str
    model: str
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int
    cost_estimate: Decimal | None
    currency: str | None
    unpriced_calls: int


class UsageOut(BaseModel):
    period_start: datetime
    ai_replies_used: int
    ai_replies_limit: int
    platform_keys_allowed: bool
    rows: list[UsageRow]


# ---- Super Admin ------------------------------------------------------------------


class AdminModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    model_id: str
    display_name: str | None
    context_window: int | None
    max_output_tokens: int | None
    active: bool
    last_seen_at: datetime | None
    current_price: PriceOut | None = None


class AdminModelCreate(BaseModel):
    provider: ProviderKey
    model_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/-]+$")
    display_name: str | None = Field(default=None, max_length=200)
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    active: bool = False


class AdminModelUpdate(BaseModel):
    active: bool | None = None
    display_name: str | None = Field(default=None, max_length=200)
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)


class ModelSyncIn(BaseModel):
    provider: ProviderKey


class ModelSyncOut(BaseModel):
    provider: str
    added: int
    updated: int


class PriceIn(BaseModel):
    input_per_mtok: Decimal = Field(ge=0, max_digits=14, decimal_places=6)
    output_per_mtok: Decimal = Field(ge=0, max_digits=14, decimal_places=6)
    cached_input_per_mtok: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    effective_from: datetime | None = None
    source_note: str | None = Field(default=None, max_length=300)


class PriceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal | None
    currency: str
    effective_from: datetime
    source_note: str | None


AdminModelOut.model_rebuild()
