"""Billing: the plan catalogue with prices (global, managed by Super Admin), one live
subscription per business, checkouts, billing details, tax invoices, payments and the
gateway webhook inbox."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Gateway(StrEnum):
    RAZORPAY = "razorpay"
    STRIPE = "stripe"


class Interval(StrEnum):
    MONTH = "month"
    YEAR = "year"


class SubscriptionStatus(StrEnum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    EXPIRED = "expired"


class SubscriptionPlan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "subscription_plans"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    # Limits are data, never code: {"max_users": 3, "max_whatsapp_numbers": 1, ...}
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    # Short lines shown on the plan card, in order.
    features: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    trial_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class PlanPrice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """What a plan costs in one currency and interval, through one gateway. A price is
    never edited once created: a new price replaces it and existing subscribers keep
    the old one until they change plan."""

    __tablename__ = "plan_prices"
    __table_args__ = (
        CheckConstraint("interval IN ('month', 'year')", name="interval_valid"),
        CheckConstraint("gateway IN ('razorpay', 'stripe')", name="gateway_valid"),
        CheckConstraint("amount > 0 AND (seat_amount IS NULL OR seat_amount > 0)", name="amounts_positive"),
        Index(
            "uq_plan_prices_active",
            "plan_id",
            "currency",
            "interval",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscription_plans.id", ondelete="RESTRICT"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    # Before tax. Tax (GST on INR) is added when the gateway plan is created.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    seat_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal(0))
    gateway: Mapped[str] = mapped_column(String(16), nullable=False)
    gateway_plan_id: Mapped[str | None] = mapped_column(String(128))
    gateway_seat_price_id: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class Subscription(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'canceled', 'expired')", name="status_valid"
        ),
        Index(
            "uq_subscriptions_one_live_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("status IN ('trialing', 'active', 'past_due')"),
        ),
        Index("ix_subscriptions_gateway_ref", "gateway", "gateway_subscription_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscription_plans.id", ondelete="RESTRICT"), nullable=False
    )
    price_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_prices.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    extra_seats: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    gateway: Mapped[str | None] = mapped_column(String(32))
    gateway_subscription_id: Mapped[str | None] = mapped_column(String(128))
    gateway_customer_id: Mapped[str | None] = mapped_column(String(128))
    # A downgrade waits for the end of the paid period.
    pending_price_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_prices.id", ondelete="RESTRICT")
    )
    past_due_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Reminder emails already sent for this subscription (trial_ending, trial_ended, ...).
    notices: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")


class Checkout(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """A payment page opened for a new subscription. The subscription starts only when
    the gateway confirms the first payment."""

    __tablename__ = "billing_checkouts"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'completed', 'expired')", name="status_valid"),
        UniqueConstraint("gateway", "gateway_ref"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    price_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_prices.id", ondelete="RESTRICT"), nullable=False
    )
    extra_seats: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gateway: Mapped[str] = mapped_column(String(16), nullable=False)
    # Razorpay subscription ID or Stripe Checkout Session ID.
    gateway_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BillingProfile(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """Who the invoices are made out to."""

    __tablename__ = "billing_profiles"
    __table_args__ = (UniqueConstraint("tenant_id"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    legal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    state_code: Mapped[str | None] = mapped_column(String(2))
    postal_code: Mapped[str | None] = mapped_column(String(16))
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    tax_id: Mapped[str | None] = mapped_column(String(64))  # VAT or EIN outside India
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class Invoice(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """A tax invoice for one successful payment. Immutable: the app role can only insert."""

    __tablename__ = "billing_invoices"
    __table_args__ = (
        UniqueConstraint("number"),
        UniqueConstraint("gateway", "gateway_payment_id"),
        Index("ix_billing_invoices_tenant_issued", "tenant_id", "issued_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="SET NULL")
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # [{"description", "sac", "quantity", "amount"}], amounts before tax
    line_items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    # [{"name": "CGST", "rate": "9", "amount": "..."}]
    taxes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    tax_note: Mapped[str | None] = mapped_column(String(300))
    seller: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    buyer: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    place_of_supply: Mapped[str | None] = mapped_column(String(100))
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gateway: Mapped[str] = mapped_column(String(16), nullable=False)
    gateway_payment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class Transaction(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """Every payment the gateways report, successful or not."""

    __tablename__ = "billing_transactions"
    __table_args__ = (
        UniqueConstraint("gateway", "gateway_payment_id"),
        CheckConstraint("status IN ('succeeded', 'failed', 'refunded')", name="status_valid"),
        Index("ix_billing_transactions_tenant_time", "tenant_id", "occurred_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="SET NULL")
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_invoices.id", ondelete="SET NULL")
    )
    gateway: Mapped[str] = mapped_column(String(16), nullable=False)
    gateway_payment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    method: Mapped[str | None] = mapped_column(String(32))
    failure_reason: Mapped[str | None] = mapped_column(String(300))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class BillingEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """Gateway webhooks, stored before the 200 and processed by a worker. tenant_id is
    NULL when the event names no business of ours (kept for diagnosis, admin only)."""

    __tablename__ = "billing_events"
    __table_args__ = (
        UniqueConstraint("gateway", "event_id"),
        CheckConstraint("status IN ('pending', 'processed', 'failed', 'ignored')", name="status_valid"),
        Index("ix_billing_events_due", "status", "received_at", postgresql_where=text("status = 'pending'")),
    )

    # Python defaults on every column: inserts never need RETURNING (see webhook_events).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    gateway: Mapped[str] = mapped_column(String(16), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InvoiceSequence(Base):
    """Consecutive invoice numbers per Indian financial year (April to March). Only the
    definer function app_next_invoice_number() writes it; the row lock it takes lasts
    until the invoice's transaction ends, so numbers have no gaps."""

    __tablename__ = "invoice_sequences"

    financial_year: Mapped[str] = mapped_column(String(7), primary_key=True)  # "2026-27"
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
