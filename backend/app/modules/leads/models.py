"""Leads: a sales opportunity with a contact, and the history of what happened to it."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    WON = "won"
    LOST = "lost"


CLOSED_STATUSES = {LeadStatus.WON, LeadStatus.LOST}


class Lead(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("status IN ('new', 'contacted', 'qualified', 'won', 'lost')", name="status_valid"),
        CheckConstraint("source IN ('whatsapp', 'manual')", name="source_valid"),
        CheckConstraint("value IS NULL OR value >= 0", name="value_not_negative"),
        Index("ix_leads_tenant_status", "tenant_id", "status", "updated_at"),
        Index("ix_leads_tenant_contact", "tenant_id", "contact_id"),
        ForeignKeyConstraint(
            ["tenant_id", "contact_id"], ["contacts.tenant_id", "contacts.id"], ondelete="CASCADE"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The conversation the lead came from, if any (same business, checked by the service).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=LeadStatus.NEW)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeadActivity(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """Append-only history: created, status_changed, owner_changed, value_changed,
    title_changed, note."""

    __tablename__ = "lead_activities"
    __table_args__ = (
        Index("ix_lead_activities_lead", "tenant_id", "lead_id", "created_at"),
        ForeignKeyConstraint(["tenant_id", "lead_id"], ["leads.tenant_id", "leads.id"], ondelete="CASCADE"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
