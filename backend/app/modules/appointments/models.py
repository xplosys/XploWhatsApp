"""Appointments: who can be booked (doctors, services, rooms), their weekly hours and
days off, the business's booking settings, and the appointments themselves."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class AppointmentStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class AppointmentSource(StrEnum):
    AI = "ai"
    STAFF = "staff"


class BookingSettings(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """One row per business. Without a row, booking is off."""

    __tablename__ = "booking_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id"),
        CheckConstraint("horizon_days BETWEEN 1 AND 90", name="horizon_range"),
        CheckConstraint("min_notice_minutes BETWEEN 0 AND 10080", name="notice_range"),
        CheckConstraint("max_upcoming_per_contact BETWEEN 1 AND 10", name="max_upcoming_range"),
        CheckConstraint("reminder_hours_before BETWEEN 1 AND 168", name="reminder_hours_range"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # The assistant books, cancels and moves appointments itself.
    ai_booking_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    min_notice_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    max_upcoming_per_contact: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # Added to every confirmation, e.g. "Please arrive 15 minutes early and bring your ID."
    confirmation_note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reminder_hours_before: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    reminder_template_name: Mapped[str | None] = mapped_column(String(512))
    reminder_template_language: Mapped[str | None] = mapped_column(String(16))


class BookableResource(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """A doctor, a service or a room that customers book time with."""

    __tablename__ = "booking_resources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("slot_minutes BETWEEN 5 AND 480", name="slot_minutes_range"),
        CheckConstraint("capacity BETWEEN 1 AND 50", name="capacity_range"),
        Index("ix_booking_resources_tenant", "tenant_id", "active", "name"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    department: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    slot_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    # How many customers can share one slot (1 for a doctor, more for a group class).
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # {"mon": [["09:00", "13:00"], ["16:00", "19:00"]], "tue": [...], ...} in the business's time zone.
    weekly_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class TimeOff(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """Whole days nobody (resource_id NULL) or one resource can be booked: holidays, leave."""

    __tablename__ = "booking_time_off"
    __table_args__ = (
        CheckConstraint("ends_on >= starts_on", name="dates_ordered"),
        Index("ix_booking_time_off_tenant", "tenant_id", "ends_on"),
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"],
            ["booking_resources.tenant_id", "booking_resources.id"],
            ondelete="CASCADE",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class Appointment(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reference"),
        CheckConstraint("status IN ('confirmed', 'cancelled', 'completed', 'no_show')", name="status_valid"),
        CheckConstraint("source IN ('ai', 'staff')", name="source_valid"),
        CheckConstraint("ends_at > starts_at", name="times_ordered"),
        CheckConstraint(
            "reminder_status IS NULL OR reminder_status IN ('sent', 'failed', 'skipped')",
            name="reminder_status_valid",
        ),
        Index("ix_appointments_tenant_starts", "tenant_id", "starts_at"),
        Index("ix_appointments_resource_starts", "tenant_id", "resource_id", "starts_at"),
        Index("ix_appointments_contact", "tenant_id", "contact_id", "starts_at"),
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"],
            ["booking_resources.tenant_id", "booking_resources.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "contact_id"], ["contacts.tenant_id", "contacts.id"], ondelete="CASCADE"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    # Short code the customer can quote, e.g. "K7P2QX".
    reference: Mapped[str] = mapped_column(String(12), nullable=False)
    patient_name: Mapped[str] = mapped_column(String(120), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AppointmentStatus.CONFIRMED)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=AppointmentSource.STAFF)
    notes: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "customer" (on WhatsApp) or "staff".
    cancelled_by: Mapped[str | None] = mapped_column(String(16))
    # How many times it was moved to another time (by the customer or staff).
    times_moved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reminder_status: Mapped[str | None] = mapped_column(String(16))
    reminder_detail: Mapped[str | None] = mapped_column(String(200))
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
