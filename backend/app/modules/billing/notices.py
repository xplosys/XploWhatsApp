"""Billing emails and the time-based work the sweeper does: trial reminders and ends,
downgrades and cancellations that wait for the end of the paid period."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import app_transaction, run_after_commit
from app.modules.billing import service
from app.modules.billing.models import PlanPrice, SubscriptionStatus

log = structlog.get_logger(__name__)


async def recipients(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    from app.modules.auth.models import User
    from app.modules.tenants.models import TenantMembership

    rows = await session.execute(
        select(User.email)
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.status == "active",
            TenantMembership.role.in_(("owner", "admin")),
        )
    )
    emails = {str(e) for (e,) in rows.all()}
    profile = await service.get_profile(session, tenant_id)
    if profile is not None and profile.email:
        emails.add(profile.email)
    return sorted(emails)


async def notify(session: AsyncSession, tenant_id: uuid.UUID, subject: str, body: str) -> None:
    from app.modules.notifications.email import OutgoingEmail, send_email

    link = f"{get_settings().frontend_base_url.rstrip('/')}/billing"
    for to in await recipients(session, tenant_id):
        email = OutgoingEmail(to=to, subject=subject, text=f"{body}\n\nBilling: {link}")

        async def _send(email: OutgoingEmail = email) -> None:
            send_email(email)

        run_after_commit(session, _send)


async def run_due(tenant_id: uuid.UUID) -> list[str]:
    """Apply what is due for one business. Safe to run repeatedly."""
    done: list[str] = []
    now = datetime.now(UTC)
    settings = get_settings()
    async with app_transaction(tenant_id) as session:
        sub = await service.current_subscription(session, tenant_id, lock=True)
        if sub is None:
            return done
        if sub.status == SubscriptionStatus.TRIALING and sub.trial_ends_at:
            if sub.trial_ends_at <= now:
                sub.status = SubscriptionStatus.EXPIRED
                sub.ended_at = now
                sub.notices = [*sub.notices, "trial_ended"]
                await notify(
                    session,
                    tenant_id,
                    "Your XploWhatsApp trial has ended",
                    "Your free trial has ended, so the AI assistant has stopped replying and new "
                    "members and documents can't be added. Choose a plan to carry on; your data is kept.",
                )
                done.append("trial_ended")
            elif (
                sub.trial_ends_at <= now + timedelta(days=settings.billing_trial_reminder_days)
                and "trial_ending" not in sub.notices
            ):
                sub.notices = [*sub.notices, "trial_ending"]
                days = max(1, round((sub.trial_ends_at - now).total_seconds() / 86400))
                await notify(
                    session,
                    tenant_id,
                    f"Your XploWhatsApp trial ends in {days} day{'s' if days != 1 else ''}",
                    "Choose a plan before your trial ends so the AI assistant keeps answering "
                    "your customers.",
                )
                done.append("trial_ending")
            return done
        if sub.current_period_end and sub.current_period_end <= now:
            if sub.cancel_at_period_end:
                sub.status = SubscriptionStatus.CANCELED
                sub.ended_at = now
                done.append("canceled")
            elif sub.pending_price_id:
                pending = await session.get(PlanPrice, sub.pending_price_id)
                if pending is not None:
                    sub.price_id, sub.plan_id = pending.id, pending.plan_id
                sub.pending_price_id = None
                done.append("downgraded")
    return done


async def sweep(limit: int = 200) -> dict[str, int]:
    """Find businesses with billing work due (through a definer function, since this
    crosses tenants) and re-send stored webhook events whose job was lost."""
    from app.workers.dispatch import BILLING_EVENT, dispatch

    settings = get_settings()
    async with app_transaction() as session:
        tenants = [
            r[0]
            for r in (
                await session.execute(
                    text("SELECT tenant_id FROM app_billing_due(:d, :l)"),
                    {"d": settings.billing_trial_reminder_days, "l": limit},
                )
            ).all()
        ]
        events = (
            await session.execute(
                text("SELECT id, tenant_id FROM app_due_billing_events(:l, 60)"), {"l": limit}
            )
        ).all()
    applied = 0
    for tenant_id in tenants:
        try:
            applied += len(await run_due(tenant_id))
        except Exception:
            log.exception("billing_due_failed", tenant_id=str(tenant_id))
    for event_id, tenant_id in events:
        await dispatch(BILLING_EVENT, event_id, tenant_id)
    return {"tenants": len(tenants), "applied": applied, "events": len(events)}
