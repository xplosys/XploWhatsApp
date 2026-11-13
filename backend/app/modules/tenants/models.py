"""Tenant-owned tables. All of them are protected by PostgreSQL Row Level Security."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class TenantRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    SUPERVISOR = "supervisor"
    AGENT = "agent"


ROLE_CHECK = "role IN ('owner', 'admin', 'supervisor', 'agent')"


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended')", name="status_valid"),
        CheckConstraint(
            "message_retention_days IS NULL OR message_retention_days BETWEEN 30 AND 3650",
            name="message_retention_days_range",
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="IN", server_default="IN")
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Kolkata", server_default="Asia/Kolkata"
    )
    default_language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="en", server_default="en"
    )
    industry: Mapped[str | None] = mapped_column(String(64))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Security and privacy settings (Phase 8).
    require_mfa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    message_retention_days: Mapped[int | None] = mapped_column(Integer)
    # Set when the owner deletes the business: data is purged after this date unless restored.
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TenantMembership(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id"),
        CheckConstraint(ROLE_CHECK, name="role_valid"),
        CheckConstraint("status IN ('active', 'suspended')", name="status_valid"),
        Index("ix_tenant_memberships_user_id", "user_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")


class TenantInvitation(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "tenant_invitations"
    __table_args__ = (
        CheckConstraint(ROLE_CHECK, name="role_valid"),
        Index("ix_tenant_invitations_tenant_email", "tenant_id", "email"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
