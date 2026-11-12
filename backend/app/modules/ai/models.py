"""AI providers, model catalogue with dated prices, assistant configuration and usage.

`ai_models` and `ai_model_prices` are platform-wide (no tenant) and read-only for the
app role; only the Super Admin console writes them. Everything else is tenant-owned
and protected by Row Level Security.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ProviderKey(StrEnum):
    GEMINI = "gemini"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"


class KeySource(StrEnum):
    TENANT = "tenant"  # the business's own API key, stored encrypted
    PLATFORM = "platform"  # XploSys's key from the environment, if the plan allows it


PROVIDERS_SQL = "('gemini', 'openai', 'deepseek')"


class AIModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A provider model that businesses may choose. Filled by syncing the provider's
    model list or by hand; only `active` models are offered."""

    __tablename__ = "ai_models"
    __table_args__ = (
        UniqueConstraint("provider", "model_id"),
        CheckConstraint(f"provider IN {PROVIDERS_SQL}", name="provider_valid"),
    )

    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    context_window: Mapped[int | None] = mapped_column(Integer)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AIModelPrice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Price per million tokens from `effective_from` until the next entry. Never edited,
    only superseded, so past cost estimates stay reproducible."""

    __tablename__ = "ai_model_prices"
    __table_args__ = (
        UniqueConstraint("model_pk", "effective_from"),
        CheckConstraint("input_per_mtok >= 0 AND output_per_mtok >= 0", name="non_negative"),
        CheckConstraint(
            "cached_input_per_mtok IS NULL OR cached_input_per_mtok >= 0", name="cached_non_negative"
        ),
    )

    model_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_models.id", ondelete="CASCADE"), nullable=False
    )
    input_per_mtok: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    output_per_mtok: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    cached_input_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_note: Mapped[str | None] = mapped_column(String(300))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class AIProviderConfig(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "ai_provider_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider"),
        CheckConstraint(f"provider IN {PROVIDERS_SQL}", name="provider_valid"),
        CheckConstraint("key_source IN ('tenant', 'platform')", name="key_source_valid"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    key_source: Mapped[str] = mapped_column(String(16), nullable=False, default=KeySource.TENANT)
    # Envelope-encrypted, bound to tenant and provider. Never returned by the API.
    api_key_ciphertext: Mapped[str | None] = mapped_column(Text)
    key_version: Mapped[str | None] = mapped_column(String(32))
    key_hint: Mapped[str | None] = mapped_column(String(16))
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_error: Mapped[str | None] = mapped_column(String(500))
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class Assistant(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """The business's WhatsApp assistant (table `bot_configurations`). One per business
    for now; its settings live in immutable versions so changes can be rolled back."""

    __tablename__ = "bot_configurations"
    __table_args__ = (UniqueConstraint("tenant_id"), UniqueConstraint("tenant_id", "id"))

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    active_version: Mapped[int | None] = mapped_column(Integer)
    auto_reply_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class AssistantVersion(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "bot_configuration_versions"
    __table_args__ = (
        UniqueConstraint("bot_configuration_id", "version"),
        ForeignKeyConstraint(
            ["tenant_id", "bot_configuration_id"],
            ["bot_configurations.tenant_id", "bot_configurations.id"],
            ondelete="CASCADE",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    bot_configuration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UsageRecord(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """Append-only metering. Metrics: `ai_call` (one provider request, with tokens and a
    cost estimate) and `ai_reply` (one AI message sent, counted against the plan)."""

    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_records_tenant_metric_time", "tenant_id", "metric", "occurred_at"),
        CheckConstraint("cost_source IN ('catalogue', 'unpriced', 'none')", name="cost_source_valid"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    cost_estimate: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    currency: Mapped[str | None] = mapped_column(String(3))
    cost_source: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
