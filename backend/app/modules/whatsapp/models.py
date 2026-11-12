"""WhatsApp Business accounts, phone numbers, encrypted credentials and templates.

All tables are tenant-owned and protected by Row Level Security. Meta identifiers
(WABA ID, phone number ID) are globally unique because webhooks are routed by them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class AccountStatus(StrEnum):
    CONNECTED = "connected"
    NEEDS_RECONNECT = "needs_reconnect"
    DISCONNECTED = "disconnected"


class AccountHealth(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    UNKNOWN = "unknown"


class NumberStatus(StrEnum):
    CONNECTED = "connected"
    PENDING = "pending"  # shared but not registered for Cloud API yet
    DISCONNECTED = "disconnected"


class CredentialKind(StrEnum):
    BUSINESS_TOKEN = "business_token"
    REGISTRATION_PIN = "registration_pin"


class WhatsAppAccount(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "whatsapp_accounts"
    __table_args__ = (
        UniqueConstraint("waba_id"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("status IN ('connected', 'needs_reconnect', 'disconnected')", name="status_valid"),
        CheckConstraint("health IN ('ok', 'warning', 'error', 'unknown')", name="health_valid"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    waba_id: Mapped[str] = mapped_column(String(64), nullable=False)
    meta_business_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(200))
    currency: Mapped[str | None] = mapped_column(String(8))
    timezone_id: Mapped[str | None] = mapped_column(String(16))
    message_template_namespace: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=AccountStatus.CONNECTED)
    health: Mapped[str] = mapped_column(String(16), nullable=False, default=AccountHealth.UNKNOWN)
    # Human-readable issues from the last health check or account webhook.
    health_issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    templates_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WhatsAppNumber(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "whatsapp_numbers"
    __table_args__ = (
        UniqueConstraint("phone_number_id"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("status IN ('connected', 'pending', 'disconnected')", name="status_valid"),
        Index("ix_whatsapp_numbers_account", "tenant_id", "account_id"),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["whatsapp_accounts.tenant_id", "whatsapp_accounts.id"],
            ondelete="CASCADE",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    phone_number_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_phone_number: Mapped[str | None] = mapped_column(String(32))
    verified_name: Mapped[str | None] = mapped_column(String(200))
    quality_rating: Mapped[str | None] = mapped_column(String(16))
    messaging_limit_tier: Mapped[str | None] = mapped_column(String(32))
    code_verification_status: Mapped[str | None] = mapped_column(String(32))
    name_status: Mapped[str | None] = mapped_column(String(32))
    platform_type: Mapped[str | None] = mapped_column(String(32))
    meta_status: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=NumberStatus.PENDING)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Number onboarded from the WhatsApp Business app (coexistence): Meta keeps it
    # registered on the app, so we must not call /register for it.
    is_coexistence: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")


class WhatsAppCredential(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """Envelope-encrypted secrets (business token, registration PIN). The plaintext
    never leaves the backend; the associated data binds each ciphertext to its row."""

    __tablename__ = "whatsapp_credentials"
    __table_args__ = (
        CheckConstraint("kind IN ('business_token', 'registration_pin')", name="kind_valid"),
        Index("ix_whatsapp_credentials_account_kind", "tenant_id", "account_id", "kind"),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["whatsapp_accounts.tenant_id", "whatsapp_accounts.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "number_id"],
            ["whatsapp_numbers.tenant_id", "whatsapp_numbers.id"],
            ondelete="CASCADE",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    number_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    ciphertext: Mapped[str | None] = mapped_column(Text)
    key_version: Mapped[str | None] = mapped_column(String(16))
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageTemplate(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "message_templates"
    __table_args__ = (
        UniqueConstraint("account_id", "name", "language"),
        Index("ix_message_templates_meta_id", "meta_template_id"),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["whatsapp_accounts.tenant_id", "whatsapp_accounts.id"],
            ondelete="CASCADE",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    meta_template_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rejected_reason: Mapped[str | None] = mapped_column(String(200))
    components: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
