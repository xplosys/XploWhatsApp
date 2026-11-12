"""Transactional inbox (webhook_events) and outbox (webhook_outbox).

Webhooks are persisted before Meta gets its 200, then processed by workers.
Outbound sends are written to the outbox in the same transaction as the message
row, so a send is never lost and never issued twice from a retry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, UUIDPrimaryKeyMixin


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WebhookEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """One row per webhook change (entry[].changes[]). tenant_id is NULL when no
    tenant owns the WABA or number yet; those rows are kept for diagnosis and are
    visible only to the admin role."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("dedup_key"),
        CheckConstraint(
            "status IN ('pending', 'processed', 'failed', 'unroutable', 'ignored')", name="status_valid"
        ),
        Index(
            "ix_webhook_events_due",
            "status",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    # Every column has a Python default so INSERTs never need RETURNING (RLS hides
    # tenant-less rows from the app role, which would make RETURNING fail).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="whatsapp")
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    waba_id: Mapped[str | None] = mapped_column(String(64))
    phone_number_id: Mapped[str | None] = mapped_column(String(64))
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxStatus:
    PENDING = "pending"
    SENDING = "sending"
    DONE = "done"
    FAILED = "failed"
    UNKNOWN = "unknown"  # worker died mid-call; not retried to avoid a duplicate send


class OutboxJob(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "webhook_outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint("status IN ('pending', 'sending', 'done', 'failed', 'unknown')", name="status_valid"),
        Index(
            "ix_webhook_outbox_due",
            "status",
            "next_attempt_at",
            postgresql_where=text("status IN ('pending', 'sending')"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # send_message
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=OutboxStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
