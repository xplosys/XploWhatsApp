"""Contacts, conversations and messages.

WhatsApp identifies a user by phone number (wa_id) and, since 2026, by a
business-scoped user ID (BSUID). Users who adopt a username may arrive with a
BSUID only, so neither identifier is mandatory; at least one is.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ConsentStatus(StrEnum):
    UNKNOWN = "unknown"
    OPTED_IN = "opted_in"
    OPTED_OUT = "opted_out"


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class SenderType(StrEnum):
    CONTACT = "contact"
    AGENT = "agent"
    AI = "ai"
    SYSTEM = "system"


class MessageStatus(StrEnum):
    RECEIVED = "received"
    QUEUED = "queued"
    SENDING = "sending"
    ACCEPTED = "accepted"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


# Status webhooks can arrive out of order; a status never moves backwards.
STATUS_RANK: dict[str, int] = {
    MessageStatus.QUEUED: 0,
    MessageStatus.SENDING: 1,
    MessageStatus.ACCEPTED: 2,
    MessageStatus.SENT: 3,
    MessageStatus.DELIVERED: 4,
    MessageStatus.READ: 5,
}


class Contact(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("wa_id IS NOT NULL OR bsuid IS NOT NULL", name="has_identifier"),
        CheckConstraint("consent_status IN ('unknown', 'opted_in', 'opted_out')", name="consent_valid"),
        Index(
            "uq_contacts_tenant_wa_id",
            "tenant_id",
            "wa_id",
            unique=True,
            postgresql_where=text("wa_id IS NOT NULL"),
        ),
        Index(
            "uq_contacts_tenant_bsuid",
            "tenant_id",
            "bsuid",
            unique=True,
            postgresql_where=text("bsuid IS NOT NULL"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # WhatsApp ID: the phone number in international format without "+".
    wa_id: Mapped[str | None] = mapped_column(String(32))
    phone_e164: Mapped[str | None] = mapped_column(String(32))
    # Business-scoped user ID, e.g. "IN.13491208655302741918". Scoped to a business portfolio.
    bsuid: Mapped[str | None] = mapped_column(String(160))
    parent_bsuid: Mapped[str | None] = mapped_column(String(160))
    username: Mapped[str | None] = mapped_column(String(64))
    profile_name: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str | None] = mapped_column(String(200))
    language: Mapped[str | None] = mapped_column(String(16))
    consent_status: Mapped[str] = mapped_column(String(16), nullable=False, default=ConsentStatus.UNKNOWN)
    consent_source: Mapped[str | None] = mapped_column(String(64))
    consent_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    previous_identifiers: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """One thread per contact per business number."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "contact_id", "number_id"),
        CheckConstraint("status IN ('open', 'closed')", name="status_valid"),
        Index("ix_conversations_inbox", "tenant_id", "status", "last_message_at"),
        ForeignKeyConstraint(
            ["tenant_id", "contact_id"], ["contacts.tenant_id", "contacts.id"], ondelete="CASCADE"
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
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    number_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    ai_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Set when the assistant handed the conversation to a person; cleared when AI is turned back on.
    needs_human: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    service_window_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="direction_valid"),
        CheckConstraint("sender_type IN ('contact', 'agent', 'ai', 'system')", name="sender_valid"),
        Index(
            "uq_messages_tenant_wa_message_id",
            "tenant_id",
            "wa_message_id",
            unique=True,
            postgresql_where=text("wa_message_id IS NOT NULL"),
        ),
        Index("ix_messages_timeline", "tenant_id", "conversation_id", "created_at"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            ondelete="CASCADE",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    sender_type: Mapped[str] = mapped_column(String(16), nullable=False)
    sent_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # text, image, audio, video, document, sticker, location, contacts, interactive,
    # button, reaction, template, system, unsupported, ...
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    # Meta's object for this message type (media IDs, template parameters, ...).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    wa_message_id: Mapped[str | None] = mapped_column(String(160))
    context_wa_message_id: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(String(500))
    pricing_category: Mapped[str | None] = mapped_column(String(32))
    billable: Mapped[bool | None] = mapped_column(Boolean)
    cost_estimate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    wa_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    template_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # For AI replies: provider, model, attempts, validation outcome and the message replied to.
    ai_provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    media_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
