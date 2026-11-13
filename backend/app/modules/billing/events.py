"""Gateway webhooks: store each event once, then let a worker apply it. Signatures are
checked before anything is stored."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import app_transaction, set_tenant_context
from app.modules.billing import service
from app.modules.billing.gateways.base import from_minor, with_tax
from app.modules.billing.gateways.razorpay import Razorpay
from app.modules.billing.gateways.razorpay import parse_subscription as razorpay_subscription
from app.modules.billing.gateways.stripe import Stripe
from app.modules.billing.gateways.stripe import parse_subscription as stripe_subscription
from app.modules.billing.models import BillingEvent, PlanPrice, Subscription, SubscriptionPlan

log = structlog.get_logger(__name__)

MAX_ATTEMPTS = 8


class RetryLater(Exception):
    """The event depends on another one that hasn't been applied yet."""


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def references(gateway: str, payload: dict[str, Any]) -> tuple[list[str], uuid.UUID | None]:
    """Gateway IDs that name one of our subscriptions or checkouts, and the tenant ID we
    put in the notes or metadata (signed by the gateway, used only as a fallback)."""
    if gateway == "razorpay":
        sub = _get(payload, "payload", "subscription", "entity") or {}
        pay = _get(payload, "payload", "payment", "entity") or {}
        refs = [r for r in (sub.get("id"), pay.get("subscription_id")) if r]
        return refs, _uuid(_get(sub, "notes", "tenant_id") or _get(pay, "notes", "tenant_id"))
    obj = _get(payload, "data", "object") or {}
    sub_ref = obj.get("subscription") or _get(obj, "parent", "subscription_details", "subscription")
    refs = [r for r in (obj.get("id"), sub_ref, obj.get("customer")) if isinstance(r, str)]
    meta = (
        _get(obj, "metadata", "tenant_id")
        or _get(obj, "subscription_details", "metadata", "tenant_id")
        or _get(obj, "parent", "subscription_details", "metadata", "tenant_id")
    )
    return refs, _uuid(meta)


async def resolve_tenant(session: AsyncSession, gateway: str, payload: dict[str, Any]) -> uuid.UUID | None:
    refs, noted = references(gateway, payload)
    for ref in refs:
        found = await session.scalar(text("SELECT app_billing_tenant(:g, :r)"), {"g": gateway, "r": ref})
        if found:
            return uuid.UUID(str(found))
    return noted


def event_identity(
    gateway: str, body: bytes, headers: dict[str, str], payload: dict[str, Any]
) -> tuple[str, str]:
    if gateway == "razorpay":
        event_id = headers.get("x-razorpay-event-id") or hashlib.sha256(body).hexdigest()
        return event_id[:128], str(payload.get("event") or "unknown")[:64]
    return str(payload.get("id") or hashlib.sha256(body).hexdigest())[:128], str(
        payload.get("type") or "unknown"
    )[:64]


async def ingest(gateway: str, body: bytes, headers: dict[str, str]) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Store a verified event. Returns (event ID, tenant ID) for the worker, or None for
    a duplicate or an event that names no business of ours."""
    try:
        payload = json.loads(body)
    except ValueError:
        log.warning("billing_webhook_invalid_json", gateway=gateway)
        return None
    if not isinstance(payload, dict):
        return None
    event_id, event_type = event_identity(gateway, body, headers, payload)
    async with app_transaction() as session:
        tenant_id = await resolve_tenant(session, gateway, payload)
        await set_tenant_context(session, tenant_id)
        row = {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "gateway": gateway,
            "event_id": event_id,
            "type": event_type,
            "payload": payload,
            "status": "pending" if tenant_id else "ignored",
            "attempts": 0,
            "received_at": datetime.now(UTC),
        }
        if tenant_id is None:
            try:
                async with session.begin_nested():
                    await session.execute(insert(BillingEvent).values(**row))
            except IntegrityError:
                pass
            log.info("billing_event_unrouted", gateway=gateway, type=event_type)
            return None
        stmt = (
            insert(BillingEvent).values(**row).on_conflict_do_nothing(index_elements=["gateway", "event_id"])
        )
        inserted = (await session.execute(stmt.returning(BillingEvent.id))).scalar_one_or_none()
    return (inserted, tenant_id) if inserted else None


async def process(event_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    async with app_transaction(tenant_id) as session:
        event = (
            await session.execute(select(BillingEvent).where(BillingEvent.id == event_id).with_for_update())
        ).scalar_one_or_none()
        if event is None or event.status != "pending":
            return "skipped"
        event.attempts += 1
        try:
            async with session.begin_nested():
                if event.gateway == "razorpay":
                    outcome = await _razorpay(session, tenant_id, event.type, event.payload)
                else:
                    outcome = await _stripe(session, tenant_id, event.type, event.payload)
        except RetryLater as exc:
            event.last_error = str(exc)[:1000]
            if event.attempts >= MAX_ATTEMPTS:
                event.status = "failed"
            return "retry"
        except Exception as exc:  # recorded and retried by the sweeper
            log.exception("billing_event_failed", event_id=str(event_id), type=event.type)
            event.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            if event.attempts >= MAX_ATTEMPTS:
                event.status = "failed"
            return "error"
        event.status = "processed" if outcome != "ignored" else "ignored"
        event.processed_at = datetime.now(UTC)
        return outcome


async def _find(
    session: AsyncSession, tenant_id: uuid.UUID, gateway: str, ref: str | None
) -> Subscription | None:
    if not ref:
        return None
    return (
        await session.execute(
            select(Subscription)
            .where(
                Subscription.tenant_id == tenant_id,
                Subscription.gateway == gateway,
                Subscription.gateway_subscription_id == ref,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _plan_lines(session: AsyncSession, sub: Subscription, total: Decimal) -> list[tuple[str, Decimal]]:
    """Razorpay payments don't itemise; the plan is one line and the rest is seats."""
    price = await session.get(PlanPrice, sub.price_id) if sub.price_id else None
    plan = await session.get(SubscriptionPlan, sub.plan_id)
    if price is None or plan is None:
        return [("XploWhatsApp subscription", total)]
    label = f"XploWhatsApp {plan.name}, {'monthly' if price.interval == 'month' else 'yearly'}"
    plan_gross = with_tax(price.amount, price.tax_rate)
    if total > plan_gross and sub.extra_seats:
        return [(label, plan_gross), (f"Extra seats ({sub.extra_seats})", total - plan_gross)]
    if total > plan_gross:
        return [(label, plan_gross), ("Adjustments", total - plan_gross)]
    return [(label, total)]


async def _razorpay(
    session: AsyncSession, tenant_id: uuid.UUID, event_type: str, payload: dict[str, Any]
) -> str:
    entity = _get(payload, "payload", "subscription", "entity")
    payment = _get(payload, "payload", "payment", "entity") or {}
    if not entity:
        if event_type == "payment.failed" and payment.get("subscription_id"):
            sub = await _find(session, tenant_id, "razorpay", payment["subscription_id"])
            if sub is None:
                return "ignored"
            if await service.record_failed_payment(
                session,
                sub,
                payment_id=str(payment["id"]),
                amount=from_minor(payment.get("amount")),
                currency=str(payment.get("currency") or "INR"),
                reason=payment.get("error_description"),
            ):
                await notify_payment_failed(session, sub)
            return "payment_failed"
        return "ignored"
    gsub = razorpay_subscription(entity)
    sub = await _find(session, tenant_id, "razorpay", gsub.ref)
    if sub is None:
        if event_type not in ("subscription.activated", "subscription.charged"):
            return "ignored"
        sub = await service.activate(session, tenant_id, gateway="razorpay", checkout_ref=gsub.ref, gsub=gsub)
        if sub is None:
            return "ignored"
    else:
        was = sub.status
        await service.sync(session, sub, gsub)
        if sub.status == "past_due" and was != "past_due":
            await notify_payment_failed(session, sub)
    if event_type == "subscription.charged" and payment.get("id"):
        total = from_minor(payment.get("amount"))
        invoice = await service.record_payment(
            session,
            sub,
            payment_id=str(payment["id"]),
            total=total,
            currency=str(payment.get("currency") or "INR"),
            method=payment.get("method"),
            lines=await _plan_lines(session, sub, total),
            period_start=gsub.period_start,
            period_end=gsub.period_end,
        )
        if invoice is not None and sub.extra_seats and sub.price_id:
            price = await session.get(PlanPrice, sub.price_id)
            if price is not None:
                await Razorpay().add_seat_addon(
                    sub, price, sub.extra_seats, Decimal(1), f"{sub.extra_seats} extra seats, next period"
                )
        return "charged"
    return event_type


async def _stripe(
    session: AsyncSession, tenant_id: uuid.UUID, event_type: str, payload: dict[str, Any]
) -> str:
    obj = _get(payload, "data", "object") or {}
    if event_type == "checkout.session.completed":
        if obj.get("mode") != "subscription" or not obj.get("subscription"):
            return "ignored"
        gsub = await Stripe().fetch(str(obj["subscription"]))
        sub = await service.activate(
            session, tenant_id, gateway="stripe", checkout_ref=str(obj["id"]), gsub=gsub
        )
        return "activated" if sub else "ignored"
    if event_type.startswith("customer.subscription."):
        gsub = stripe_subscription(obj)
        sub = await _find(session, tenant_id, "stripe", gsub.ref)
        if sub is None:
            return "ignored"
        was = sub.status
        await service.sync(session, sub, gsub)
        if sub.status == "past_due" and was != "past_due":
            await notify_payment_failed(session, sub)
        return event_type
    if event_type in ("invoice.paid", "invoice.payment_failed"):
        ref = obj.get("subscription") or _get(obj, "parent", "subscription_details", "subscription")
        if not ref:
            return "ignored"
        sub = await _find(session, tenant_id, "stripe", str(ref))
        if sub is None:
            raise RetryLater("subscription not activated yet")
        currency = str(obj.get("currency") or "").upper()
        if event_type == "invoice.payment_failed":
            if await service.record_failed_payment(
                session,
                sub,
                payment_id=f"{obj['id']}:{obj.get('attempt_count') or 1}",
                amount=from_minor(obj.get("amount_due")),
                currency=currency,
                reason=_get(obj, "last_finalization_error", "message"),
            ):
                await notify_payment_failed(session, sub)
            return "payment_failed"
        lines = [
            (str(line.get("description") or "Subscription"), from_minor(line.get("amount")))
            for line in (_get(obj, "lines", "data") or [])
            if int(line.get("amount") or 0) > 0
        ]
        first = (_get(obj, "lines", "data") or [{}])[0]
        start, end = _get(first, "period", "start"), _get(first, "period", "end")
        await service.record_payment(
            session,
            sub,
            payment_id=str(obj["id"]),
            total=from_minor(obj.get("amount_paid")),
            currency=currency,
            method="card",
            lines=lines,
            period_start=datetime.fromtimestamp(int(start), UTC) if start else None,
            period_end=datetime.fromtimestamp(int(end), UTC) if end else None,
        )
        return "paid"
    return "ignored"


async def notify_payment_failed(session: AsyncSession, sub: Subscription) -> None:
    from app.modules.billing.notices import notify

    await notify(
        session,
        sub.tenant_id,
        "Your XploWhatsApp payment didn't go through",
        "We couldn't collect your latest XploWhatsApp payment. The payment provider will try again; "
        "please check your card or mandate on the Billing page so your plan keeps working.",
    )
