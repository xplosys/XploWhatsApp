"""Subscriptions: trials, the plan a business has right now, checkout, plan and seat
changes, cancellation, payments and tax invoices. Gateways are reached through the
PaymentGateway interface; gateway webhooks are turned into calls here by events.py."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, Conflict, NotFound, ValidationFailed
from app.modules.billing import gst
from app.modules.billing.gateways.base import (
    CheckoutRequest,
    GatewaySubscription,
    gateway_for_currency,
    get_gateway,
    money,
)
from app.modules.billing.models import (
    BillingProfile,
    Checkout,
    Interval,
    Invoice,
    PlanPrice,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    Transaction,
)

log = structlog.get_logger(__name__)

TRIAL_PLAN_CODE = "trial"
LIVE_STATUSES = ("trialing", "active", "past_due")


class PlanCatalogueMissing(AppError):
    status_code = 503
    code = "plan_catalogue_missing"
    message = "Plans are not configured yet. Run the seed command."


def _now() -> datetime:
    return datetime.now(UTC)


# ---- trial and the plan in force -------------------------------------------------------


async def start_trial(session: AsyncSession, tenant_id: uuid.UUID) -> Subscription:
    plan = (
        await session.execute(
            select(SubscriptionPlan).where(
                SubscriptionPlan.code == TRIAL_PLAN_CODE, SubscriptionPlan.is_active
            )
        )
    ).scalar_one_or_none()
    if plan is None:
        raise PlanCatalogueMissing()
    now = _now()
    sub = Subscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        status=SubscriptionStatus.TRIALING,
        trial_ends_at=now + timedelta(days=plan.trial_days),
        current_period_start=now,
        current_period_end=now + timedelta(days=plan.trial_days),
        notices=[],
    )
    session.add(sub)
    return sub


def in_force(sub: Subscription, now: datetime | None = None) -> bool:
    """Whether the subscription still gives access, even before the sweeper has
    recorded an ended trial, an expired grace period or a cancellation."""
    now = now or _now()
    if sub.status not in LIVE_STATUSES:
        return False
    if sub.status == SubscriptionStatus.TRIALING and sub.trial_ends_at and sub.trial_ends_at <= now:
        return False
    if sub.status == SubscriptionStatus.PAST_DUE and sub.past_due_since:
        if sub.past_due_since + timedelta(days=get_settings().billing_grace_days) <= now:
            return False
    return not (sub.cancel_at_period_end and sub.current_period_end and sub.current_period_end <= now)


@dataclass
class EffectivePlan:
    subscription: Subscription | None
    plan: SubscriptionPlan | None

    @property
    def usable(self) -> bool:
        return self.subscription is not None and self.plan is not None and in_force(self.subscription)

    def limit(self, key: str, default: int = 0) -> int:
        if not self.usable:
            return default
        assert self.plan is not None
        value: Any = self.plan.limits.get(key, default)
        if key == "max_users" and self.subscription is not None:
            value = int(value) + self.subscription.extra_seats
        return int(value)


async def current_subscription(
    session: AsyncSession, tenant_id: uuid.UUID, *, lock: bool = False
) -> Subscription | None:
    q = select(Subscription).where(
        Subscription.tenant_id == tenant_id, Subscription.status.in_(LIVE_STATUSES)
    )
    if lock:
        q = q.with_for_update()
    return (await session.execute(q)).scalar_one_or_none()


async def effective_plan(session: AsyncSession, tenant_id: uuid.UUID) -> EffectivePlan:
    """The live subscription and its plan. Limits are 0 (or the caller's default) once a
    trial has ended, a failed payment's grace period is over or a cancellation applies."""
    row = (
        await session.execute(
            select(Subscription, SubscriptionPlan)
            .join(SubscriptionPlan, SubscriptionPlan.id == Subscription.plan_id)
            .where(Subscription.tenant_id == tenant_id, Subscription.status.in_(LIVE_STATUSES))
        )
    ).first()
    if row is None:
        return EffectivePlan(None, None)
    return EffectivePlan(row[0], row[1])


# ---- catalogue ------------------------------------------------------------------------------


def tax_rate_for(currency: str) -> Decimal:
    """GST applies to INR sales when the seller is registered; other currencies are
    exports of services (zero-rated)."""
    s = get_settings()
    rate = s.gst_rate_percent if currency == "INR" and s.seller_gstin else Decimal(0)
    return rate.quantize(Decimal("0.01"))


def currencies_for(country: str) -> tuple[str, ...]:
    return ("INR",) if country == "IN" else ("USD", "GBP", "EUR")


async def get_price(session: AsyncSession, price_id: uuid.UUID) -> tuple[PlanPrice, SubscriptionPlan]:
    row = (
        await session.execute(
            select(PlanPrice, SubscriptionPlan)
            .join(SubscriptionPlan, SubscriptionPlan.id == PlanPrice.plan_id)
            .where(PlanPrice.id == price_id)
        )
    ).first()
    if row is None:
        raise NotFound("That price was not found.", code="price_not_found")
    return row[0], row[1]


def per_month(price: PlanPrice) -> Decimal:
    return price.amount if price.interval == Interval.MONTH else price.amount / 12


# ---- usage and limits ---------------------------------------------------------------------

LIMIT_LABELS = {
    "max_users": "Team members",
    "max_whatsapp_numbers": "WhatsApp numbers",
    "max_knowledge_documents": "Knowledge documents",
    "max_storage_mb": "Knowledge storage (MB)",
    "monthly_ai_messages": "AI replies this month",
}


async def usage(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    """What the business uses, counted the same way each limit is enforced."""
    from app.modules.ai.catalogue import replies_this_month
    from app.modules.knowledge.service import usage as knowledge_usage
    from app.modules.tenants.models import TenantMembership
    from app.modules.whatsapp.models import NumberStatus, WhatsAppNumber

    members = await session.scalar(
        select(func.count()).where(
            TenantMembership.tenant_id == tenant_id, TenantMembership.status == "active"
        )
    )
    numbers = await session.scalar(
        select(func.count()).where(
            WhatsAppNumber.tenant_id == tenant_id, WhatsAppNumber.status == NumberStatus.CONNECTED
        )
    )
    documents, stored = await knowledge_usage(session, tenant_id)
    return {
        "max_users": int(members or 0),
        "max_whatsapp_numbers": int(numbers or 0),
        "max_knowledge_documents": documents,
        "max_storage_mb": -(-stored // (1024 * 1024)),
        "monthly_ai_messages": await replies_this_month(session, tenant_id),
    }


async def what_does_not_fit(
    session: AsyncSession, tenant_id: uuid.UUID, plan: SubscriptionPlan, extra_seats: int
) -> list[str]:
    """What the business uses today that the plan would not allow (AI replies excluded:
    they reset monthly)."""
    used = await usage(session, tenant_id)
    problems = []
    for key in ("max_users", "max_whatsapp_numbers", "max_knowledge_documents", "max_storage_mb"):
        limit = int(plan.limits.get(key, 0)) + (extra_seats if key == "max_users" else 0)
        if used[key] > limit:
            problems.append(f"{LIMIT_LABELS[key]}: you use {used[key]}, the plan allows {limit}")
    return problems


# ---- billing details ----------------------------------------------------------------------


async def get_profile(session: AsyncSession, tenant_id: uuid.UUID) -> BillingProfile | None:
    return (
        await session.execute(select(BillingProfile).where(BillingProfile.tenant_id == tenant_id))
    ).scalar_one_or_none()


def check_profile_for(profile: BillingProfile | None, currency: str) -> None:
    if profile is None or not profile.legal_name:
        raise ValidationFailed("Add your billing details first.", code="billing_details_missing")
    if currency == "INR" and not profile.state_code:
        raise ValidationFailed(
            "Add your state to your billing details: it decides which GST applies.",
            code="billing_state_missing",
        )


# ---- checkout -------------------------------------------------------------------------------


@dataclass
class CheckoutResult:
    checkout: Checkout
    gateway: str
    client: dict[str, Any]


async def start_checkout(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    country: str,
    user_id: uuid.UUID,
    email: str | None,
    name: str | None,
    price_id: uuid.UUID,
    extra_seats: int,
) -> CheckoutResult:
    price, plan = await get_price(session, price_id)
    if not (price.is_active and plan.is_active and plan.is_public):
        raise ValidationFailed("That plan is not available.", code="price_unavailable")
    if price.currency not in currencies_for(country):
        raise ValidationFailed("That price is not offered in your country.", code="currency_not_offered")
    if extra_seats and not price.seat_amount:
        raise ValidationFailed("This plan doesn't offer extra seats.", code="seats_not_offered")
    current = await current_subscription(session, tenant_id)
    if current is not None and current.gateway_subscription_id and in_force(current):
        raise Conflict("You already have a paid plan. Change plan instead.", code="already_subscribed")
    profile = await get_profile(session, tenant_id)
    check_profile_for(profile, price.currency)
    problems = await what_does_not_fit(session, tenant_id, plan, extra_seats)
    if problems:
        raise ValidationFailed(
            "This plan is smaller than what you use now.", code="plan_too_small", details=problems
        )
    gateway = get_gateway(price.gateway)
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    started = await gateway.start_checkout(
        CheckoutRequest(
            tenant_id=tenant_id,
            price=price,
            plan=plan,
            extra_seats=extra_seats,
            email=(profile.email if profile else None) or email,
            name=(profile.legal_name if profile else None) or name,
            phone=None,
            customer_id=current.gateway_customer_id if current else None,
            success_url=f"{base}/billing?checkout=success",
            cancel_url=f"{base}/billing?checkout=canceled",
        )
    )
    checkout = Checkout(
        tenant_id=tenant_id,
        price_id=price.id,
        extra_seats=extra_seats,
        gateway=price.gateway,
        gateway_ref=started.ref,
        status="open",
        created_by_user_id=user_id,
    )
    session.add(checkout)
    await session.flush()
    return CheckoutResult(checkout, price.gateway, started.client)


async def activate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    gateway: str,
    checkout_ref: str,
    gsub: GatewaySubscription,
) -> Subscription | None:
    """The gateway confirmed a checkout: end the trial (or an ended plan) and start the
    paid subscription. Idempotent."""
    existing = (
        await session.execute(
            select(Subscription)
            .where(
                Subscription.tenant_id == tenant_id,
                Subscription.gateway == gateway,
                Subscription.gateway_subscription_id == gsub.ref,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        await sync(session, existing, gsub)
        return existing
    checkout = (
        await session.execute(
            select(Checkout)
            .where(
                Checkout.tenant_id == tenant_id,
                Checkout.gateway == gateway,
                Checkout.gateway_ref == checkout_ref,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if checkout is None:
        log.warning("billing_unknown_checkout", gateway=gateway, ref=checkout_ref)
        return None
    if gsub.status not in ("active", "past_due"):
        return None  # not paid yet
    price, plan = await get_price(session, checkout.price_id)
    now = _now()
    current = await current_subscription(session, tenant_id, lock=True)
    if current is not None:
        current.status = (
            SubscriptionStatus.EXPIRED
            if current.status == SubscriptionStatus.TRIALING
            else SubscriptionStatus.CANCELED
        )
        current.ended_at = now
        await session.flush()
    sub = Subscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        price_id=price.id,
        status=SubscriptionStatus.ACTIVE,
        current_period_start=gsub.period_start or now,
        current_period_end=gsub.period_end,
        extra_seats=checkout.extra_seats,
        gateway=gateway,
        gateway_subscription_id=gsub.ref,
        gateway_customer_id=gsub.customer_id,
        notices=[],
    )
    session.add(sub)
    checkout.status = "completed"
    checkout.completed_at = now
    await session.flush()
    from app.modules.audit.service import record

    await record(
        session,
        "billing.subscription_started",
        tenant_id=tenant_id,
        actor_user_id=checkout.created_by_user_id,
        target_type="subscription",
        target_id=sub.id,
        details={
            "plan": plan.code,
            "interval": price.interval,
            "currency": price.currency,
            "gateway": gateway,
        },
    )
    return sub


async def sync(session: AsyncSession, sub: Subscription, gsub: GatewaySubscription) -> None:
    """Bring our record in line with what the gateway reports."""
    now = _now()
    paid_until = sub.current_period_end
    if gsub.period_start:
        sub.current_period_start = gsub.period_start
    if gsub.period_end:
        sub.current_period_end = gsub.period_end
    if gsub.customer_id and not sub.gateway_customer_id:
        sub.gateway_customer_id = gsub.customer_id
    if gsub.gateway_plan_id and sub.price_id:
        current = await session.get(PlanPrice, sub.price_id)
        if current is not None and gsub.gateway_plan_id != current.gateway_plan_id:
            moved = (
                await session.execute(
                    select(PlanPrice).where(
                        PlanPrice.gateway == sub.gateway, PlanPrice.gateway_plan_id == gsub.gateway_plan_id
                    )
                )
            ).scalar_one_or_none()
            # Stripe moves a scheduled downgrade's item straight away (without proration);
            # the business keeps the plan it paid for until the period it paid for ends.
            waiting = (
                moved is not None and moved.id == sub.pending_price_id and paid_until and now < paid_until
            )
            if moved is not None and not waiting:
                sub.price_id, sub.plan_id = moved.id, moved.plan_id
                if sub.pending_price_id == moved.id:
                    sub.pending_price_id = None
    if gsub.status == "active":
        if sub.status != SubscriptionStatus.ACTIVE:
            sub.status = SubscriptionStatus.ACTIVE
        sub.past_due_since = None
        if sub.gateway == "stripe":  # Razorpay doesn't report a scheduled cancellation
            sub.cancel_at_period_end = gsub.cancel_at_period_end
    elif gsub.status == "past_due":
        if sub.status != SubscriptionStatus.PAST_DUE:
            sub.status = SubscriptionStatus.PAST_DUE
            sub.past_due_since = sub.past_due_since or now
    elif gsub.status == "canceled":
        sub.status = SubscriptionStatus.CANCELED
        sub.ended_at = sub.ended_at or now
        sub.canceled_at = sub.canceled_at or now


# ---- payments and invoices ------------------------------------------------------------------


def seller_snapshot() -> dict[str, Any]:
    s = get_settings()
    return {
        "legal_name": s.seller_legal_name,
        "address": s.seller_address,
        "gstin": s.seller_gstin,
        "state_code": s.seller_state_code,
        "state": gst.STATES.get(s.seller_state_code or ""),
        "lut_number": s.seller_lut_number,
    }


def buyer_snapshot(profile: BillingProfile | None, fallback_name: str) -> dict[str, Any]:
    if profile is None:
        return {"legal_name": fallback_name}
    return {
        "legal_name": profile.legal_name,
        "email": profile.email,
        "address_line1": profile.address_line1,
        "address_line2": profile.address_line2,
        "city": profile.city,
        "state": profile.state,
        "state_code": profile.state_code,
        "postal_code": profile.postal_code,
        "country": profile.country,
        "gstin": profile.gstin,
        "tax_id": profile.tax_id,
    }


async def record_payment(
    session: AsyncSession,
    sub: Subscription,
    *,
    payment_id: str,
    total: Decimal,
    currency: str,
    method: str | None,
    lines: list[tuple[str, Decimal]],
    period_start: datetime | None,
    period_end: datetime | None,
) -> Invoice | None:
    """A successful payment: one transaction and one tax invoice. Idempotent per payment."""
    assert sub.gateway
    seen = await session.scalar(
        select(Transaction.id).where(
            Transaction.gateway == sub.gateway, Transaction.gateway_payment_id == payment_id
        )
    )
    if seen is not None:
        return None
    total = money(total)
    invoice = None
    if total > 0:
        invoice = await _issue_invoice(
            session,
            sub,
            payment_id=payment_id,
            total=total,
            currency=currency,
            lines=lines,
            period_start=period_start,
            period_end=period_end,
        )
    session.add(
        Transaction(
            tenant_id=sub.tenant_id,
            subscription_id=sub.id,
            invoice_id=invoice.id if invoice else None,
            gateway=sub.gateway,
            gateway_payment_id=payment_id,
            amount=total,
            currency=currency,
            status="succeeded",
            method=method,
        )
    )
    if sub.status == SubscriptionStatus.PAST_DUE:
        sub.status = SubscriptionStatus.ACTIVE
        sub.past_due_since = None
    await session.flush()
    return invoice


async def record_failed_payment(
    session: AsyncSession,
    sub: Subscription,
    *,
    payment_id: str,
    amount: Decimal,
    currency: str,
    reason: str | None,
) -> bool:
    assert sub.gateway
    seen = await session.scalar(
        select(Transaction.id).where(
            Transaction.gateway == sub.gateway, Transaction.gateway_payment_id == payment_id
        )
    )
    if seen is not None:
        return False
    session.add(
        Transaction(
            tenant_id=sub.tenant_id,
            subscription_id=sub.id,
            gateway=sub.gateway,
            gateway_payment_id=payment_id,
            amount=money(amount),
            currency=currency,
            status="failed",
            failure_reason=(reason or "")[:300] or None,
        )
    )
    if sub.status == SubscriptionStatus.ACTIVE:
        sub.status = SubscriptionStatus.PAST_DUE
        sub.past_due_since = _now()
    await session.flush()
    return True


async def _issue_invoice(
    session: AsyncSession,
    sub: Subscription,
    *,
    payment_id: str,
    total: Decimal,
    currency: str,
    lines: list[tuple[str, Decimal]],
    period_start: datetime | None,
    period_end: datetime | None,
) -> Invoice:
    from app.modules.tenants.models import Tenant

    assert sub.gateway
    settings = get_settings()
    price = await session.get(PlanPrice, sub.price_id) if sub.price_id else None
    rate = price.tax_rate if price is not None else tax_rate_for(currency)
    profile = await get_profile(session, sub.tenant_id)
    tenant = await session.get(Tenant, sub.tenant_id)
    buyer_state = profile.state_code if profile else None
    subtotal, taxes = gst.split_tax(total, rate, settings.seller_state_code, buyer_state)

    # Lines arrive tax-inclusive; spread the subtotal over them and fix rounding on the last.
    gross = sum((amount for _, amount in lines), Decimal(0)) or total
    items: list[dict[str, Any]] = []
    running = Decimal(0)
    source = lines or [("Subscription", total)]
    for i, (description, amount) in enumerate(source):
        part = subtotal - running if i == len(source) - 1 else money(subtotal * amount / gross)
        running += part
        items.append(
            {"description": description, "sac": settings.invoice_sac_code, "quantity": 1, "amount": str(part)}
        )

    note = None
    if currency != "INR" and settings.seller_gstin:
        note = "Supply meant for export of services without payment of IGST" + (
            f" under LUT {settings.seller_lut_number}" if settings.seller_lut_number else ""
        )
    issued = _now()
    year = gst.financial_year(issued)
    n = int(await session.scalar(text("SELECT app_next_invoice_number(:y)"), {"y": year}) or 0)
    invoice = Invoice(
        tenant_id=sub.tenant_id,
        subscription_id=sub.id,
        number=f"{settings.invoice_prefix}/{year}/{n:06d}",
        currency=currency,
        subtotal=subtotal,
        tax_total=money(total - subtotal),
        total=total,
        line_items=items,
        taxes=taxes,
        tax_note=note,
        seller=seller_snapshot(),
        buyer=buyer_snapshot(profile, tenant.name if tenant else ""),
        place_of_supply=(
            f"{gst.STATES.get(buyer_state, buyer_state)} ({buyer_state})"
            if currency == "INR" and buyer_state
            else None
        ),
        period_start=period_start,
        period_end=period_end,
        gateway=sub.gateway,
        gateway_payment_id=payment_id,
        issued_at=issued,
    )
    session.add(invoice)
    await session.flush()
    return invoice


# ---- changes to a paid subscription -------------------------------------------------------


async def paid_subscription(session: AsyncSession, tenant_id: uuid.UUID) -> tuple[Subscription, PlanPrice]:
    sub = await current_subscription(session, tenant_id, lock=True)
    if sub is None or not sub.gateway_subscription_id or not sub.price_id:
        raise Conflict("Choose a plan first.", code="no_paid_plan")
    price = await session.get(PlanPrice, sub.price_id)
    assert price is not None
    return sub, price


def fraction_left(sub: Subscription) -> Decimal:
    if not (sub.current_period_start and sub.current_period_end):
        return Decimal(1)
    total = (sub.current_period_end - sub.current_period_start).total_seconds()
    left = (sub.current_period_end - _now()).total_seconds()
    if total <= 0:
        return Decimal(0)
    return Decimal(str(max(0.0, min(1.0, left / total)))).quantize(Decimal("0.0001"))


async def change_plan(
    session: AsyncSession, tenant_id: uuid.UUID, new_price_id: uuid.UUID
) -> tuple[Subscription, bool]:
    """Upgrades (more per month, or monthly to yearly) apply now with proration; smaller
    plans apply at the end of the paid period. Returns (subscription, immediate)."""
    sub, old = await paid_subscription(session, tenant_id)
    if sub.status == SubscriptionStatus.PAST_DUE:
        raise Conflict("Settle the failed payment before changing plan.", code="payment_due")
    if sub.cancel_at_period_end:
        raise Conflict("Your plan is set to end. Resume it before changing plan.", code="cancel_scheduled")
    new, plan = await get_price(session, new_price_id)
    if not (new.is_active and plan.is_active and plan.is_public):
        raise ValidationFailed("That plan is not available.", code="price_unavailable")
    if new.id == old.id:
        raise ValidationFailed("You are already on this plan.", code="same_plan")
    if new.currency != old.currency or new.gateway != old.gateway:
        raise ValidationFailed(
            "Plans can only be changed within the same currency.", code="currency_mismatch"
        )
    if sub.extra_seats and not new.seat_amount:
        raise ValidationFailed(
            "This plan has no extra seats. Remove your extra seats first.", code="seats_not_offered"
        )
    problems = await what_does_not_fit(session, tenant_id, plan, sub.extra_seats)
    if problems:
        raise ValidationFailed(
            "This plan is smaller than what you use now.", code="plan_too_small", details=problems
        )
    immediate = per_month(new) > per_month(old) or (
        old.interval == Interval.MONTH and new.interval == Interval.YEAR
    )
    await get_gateway(sub.gateway or "").change_price(sub, old, new, immediate=immediate)
    if immediate:
        sub.price_id, sub.plan_id = new.id, new.plan_id
        sub.pending_price_id = None
    else:
        sub.pending_price_id = new.id
    await session.flush()
    return sub, immediate


async def undo_pending_change(session: AsyncSession, tenant_id: uuid.UUID) -> Subscription:
    sub, current = await paid_subscription(session, tenant_id)
    if sub.pending_price_id is None:
        raise Conflict("There is no plan change waiting.", code="no_pending_change")
    pending = await session.get(PlanPrice, sub.pending_price_id)
    assert pending is not None
    gateway = get_gateway(sub.gateway or "")
    if sub.gateway == "razorpay":
        from app.modules.billing.gateways.razorpay import Razorpay

        assert isinstance(gateway, Razorpay)
        await gateway.cancel_scheduled_change(sub)
    else:
        await gateway.change_price(sub, pending, current, immediate=False)
    sub.pending_price_id = None
    await session.flush()
    return sub


async def set_seats(session: AsyncSession, tenant_id: uuid.UUID, extra_seats: int) -> Subscription:
    sub, price = await paid_subscription(session, tenant_id)
    if not price.seat_amount:
        raise ValidationFailed("Your plan doesn't offer extra seats.", code="seats_not_offered")
    plan = await session.get(SubscriptionPlan, sub.plan_id)
    assert plan is not None
    members = (await usage(session, tenant_id))["max_users"]
    needed = max(0, members - int(plan.limits.get("max_users", 0)))
    if extra_seats < needed:
        raise ValidationFailed(
            f"You have {members} active members, so you need at least {needed} extra seats. "
            "Remove members first to go lower.",
            code="seats_in_use",
        )
    if extra_seats == sub.extra_seats:
        return sub
    await get_gateway(sub.gateway or "").set_seats(
        sub, price, sub.extra_seats, extra_seats, period_fraction_left=fraction_left(sub)
    )
    sub.extra_seats = extra_seats
    await session.flush()
    return sub


async def cancel(session: AsyncSession, tenant_id: uuid.UUID) -> Subscription:
    sub, _ = await paid_subscription(session, tenant_id)
    if sub.cancel_at_period_end:
        return sub
    await get_gateway(sub.gateway or "").cancel(sub, at_period_end=True)
    sub.cancel_at_period_end = True
    sub.canceled_at = _now()
    sub.pending_price_id = None
    await session.flush()
    return sub


async def resume(session: AsyncSession, tenant_id: uuid.UUID) -> Subscription:
    sub, _ = await paid_subscription(session, tenant_id)
    if not sub.cancel_at_period_end:
        return sub
    await get_gateway(sub.gateway or "").resume(sub)
    sub.cancel_at_period_end = False
    sub.canceled_at = None
    await session.flush()
    return sub


def default_gateway(currency: str) -> str:
    return gateway_for_currency(currency)
