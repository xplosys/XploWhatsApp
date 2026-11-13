"""Billing for a business: plan, usage against limits, checkout, plan and seat changes,
cancellation, billing details, invoices and payments. Plus the gateway webhooks."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select

from app.api.deps import Meta, Requires, TenantContext
from app.core.errors import AppError, NotFound, Unauthorized, ValidationFailed
from app.modules.audit.service import record
from app.modules.billing import events, gst, service
from app.modules.billing.gateways.base import get_gateway, with_tax
from app.modules.billing.gateways.razorpay import Razorpay
from app.modules.billing.gateways.stripe import Stripe
from app.modules.billing.models import (
    BillingProfile,
    Invoice,
    PlanPrice,
    Subscription,
    SubscriptionPlan,
    Transaction,
)
from app.modules.rbac.permissions import Permission
from app.workers.dispatch import BILLING_EVENT, dispatch

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])
webhooks = APIRouter(prefix="/webhooks", tags=["billing"])

Reader = Annotated[TenantContext, Requires(Permission.BILLING_READ)]
Manager = Annotated[TenantContext, Requires(Permission.BILLING_MANAGE)]
MAX_WEBHOOK_BYTES = 1_000_000


# ---- schemas ------------------------------------------------------------------------------


class PriceOut(BaseModel):
    id: uuid.UUID
    currency: str
    interval: str
    amount: Decimal
    seat_amount: Decimal | None
    tax_rate: Decimal
    amount_with_tax: Decimal
    gateway: str


class PlanOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    features: list[str]
    limits: dict[str, Any]
    prices: list[PriceOut]


class PlansOut(BaseModel):
    currencies: list[str]
    plans: list[PlanOut]
    payments_ready: bool


class SubscriptionOut(BaseModel):
    status: str | None
    in_force: bool
    plan_code: str | None
    plan_name: str | None
    limits: dict[str, Any]
    price: PriceOut | None
    pending_price: PriceOut | None
    pending_plan_name: str | None
    trial_ends_at: datetime | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    past_due_since: datetime | None
    grace_ends_at: datetime | None
    extra_seats: int
    seats_used: int
    seats_included: int
    gateway: str | None


class UsageOut(BaseModel):
    key: str
    label: str
    used: int
    limit: int


class ProfileIn(BaseModel):
    legal_name: str = Field(min_length=2, max_length=200)
    email: EmailStr | None = None
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    state_code: str | None = Field(default=None, max_length=2)
    state: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, max_length=16)
    gstin: str | None = Field(default=None, max_length=15)
    tax_id: str | None = Field(default=None, max_length=64)

    @field_validator(
        "gstin", "state_code", "tax_id", "state", "city", "postal_code", "address_line1", "address_line2"
    )
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return v.strip() or None if v is not None else None


class ProfileOut(BaseModel):
    legal_name: str
    email: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    state_code: str | None
    postal_code: str | None
    country: str
    gstin: str | None
    tax_id: str | None
    updated_at: datetime


class CheckoutIn(BaseModel):
    price_id: uuid.UUID
    extra_seats: int = Field(default=0, ge=0, le=500)


class CheckoutOut(BaseModel):
    checkout_id: uuid.UUID
    gateway: str
    razorpay: dict[str, Any] | None = None
    stripe_url: str | None = None


class RazorpayVerifyIn(BaseModel):
    razorpay_payment_id: str = Field(max_length=64)
    razorpay_subscription_id: str = Field(max_length=64)
    razorpay_signature: str = Field(max_length=128)


class StripeConfirmIn(BaseModel):
    session_id: str = Field(max_length=200)


class ChangeIn(BaseModel):
    price_id: uuid.UUID


class ChangeOut(BaseModel):
    immediate: bool
    subscription: SubscriptionOut


class SeatsIn(BaseModel):
    extra_seats: int = Field(ge=0, le=500)


class InvoiceSummary(BaseModel):
    id: uuid.UUID
    number: str
    currency: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    issued_at: datetime
    period_start: datetime | None
    period_end: datetime | None


class InvoiceOut(InvoiceSummary):
    line_items: list[dict[str, Any]]
    taxes: list[dict[str, Any]]
    tax_note: str | None
    seller: dict[str, Any]
    buyer: dict[str, Any]
    place_of_supply: str | None
    gateway: str
    gateway_payment_id: str


class TransactionOut(BaseModel):
    id: uuid.UUID
    amount: Decimal
    currency: str
    status: str
    method: str | None
    failure_reason: str | None
    invoice_id: uuid.UUID | None
    occurred_at: datetime


# ---- helpers ------------------------------------------------------------------------------


def price_out(p: PlanPrice) -> PriceOut:
    return PriceOut(
        id=p.id,
        currency=p.currency,
        interval=p.interval,
        amount=p.amount,
        seat_amount=p.seat_amount,
        tax_rate=p.tax_rate,
        amount_with_tax=with_tax(p.amount, p.tax_rate),
        gateway=p.gateway,
    )


async def subscription_out(ctx: TenantContext) -> SubscriptionOut:
    from app.core.config import get_settings

    s = ctx.session
    eff = await service.effective_plan(s, ctx.tenant_id)
    used = (await service.usage(s, ctx.tenant_id))["max_users"]
    sub = eff.subscription
    price = await s.get(PlanPrice, sub.price_id) if sub and sub.price_id else None
    pending = await s.get(PlanPrice, sub.pending_price_id) if sub and sub.pending_price_id else None
    pending_plan = await s.get(SubscriptionPlan, pending.plan_id) if pending else None
    grace = None
    if sub and sub.past_due_since:
        from datetime import timedelta

        grace = sub.past_due_since + timedelta(days=get_settings().billing_grace_days)
    included = 0
    if eff.plan is not None:
        included = int(eff.plan.limits.get("max_users", 0)) + (sub.extra_seats if sub else 0)
    return SubscriptionOut(
        status=sub.status if sub else None,
        in_force=eff.usable,
        plan_code=eff.plan.code if eff.plan else None,
        plan_name=eff.plan.name if eff.plan else None,
        limits=eff.plan.limits if eff.plan else {},
        price=price_out(price) if price else None,
        pending_price=price_out(pending) if pending else None,
        pending_plan_name=pending_plan.name if pending_plan else None,
        trial_ends_at=sub.trial_ends_at if sub else None,
        current_period_start=sub.current_period_start if sub else None,
        current_period_end=sub.current_period_end if sub else None,
        cancel_at_period_end=sub.cancel_at_period_end if sub else False,
        past_due_since=sub.past_due_since if sub else None,
        grace_ends_at=grace,
        extra_seats=sub.extra_seats if sub else 0,
        seats_used=used,
        seats_included=included,
        gateway=sub.gateway if sub else None,
    )


async def _audit(ctx: TenantContext, meta: Any, action: str, details: dict[str, Any] | None = None) -> None:
    sub = await service.current_subscription(ctx.session, ctx.tenant_id)
    await record(
        ctx.session,
        action,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="subscription",
        target_id=sub.id if sub else None,
        details=details,
    )


# ---- plan, usage and catalogue --------------------------------------------------------------


@router.get("/subscription", response_model=SubscriptionOut)
async def get_subscription(ctx: Reader) -> SubscriptionOut:
    return await subscription_out(ctx)


@router.get("/usage", response_model=list[UsageOut])
async def get_usage(ctx: Reader) -> list[UsageOut]:
    eff = await service.effective_plan(ctx.session, ctx.tenant_id)
    used = await service.usage(ctx.session, ctx.tenant_id)
    return [
        UsageOut(key=k, label=label, used=used[k], limit=eff.limit(k, 0))
        for k, label in service.LIMIT_LABELS.items()
    ]


@router.get("/plans", response_model=PlansOut)
async def list_plans(ctx: Reader) -> PlansOut:
    currencies = service.currencies_for(ctx.tenant.country)
    plans = (
        (
            await ctx.session.execute(
                select(SubscriptionPlan)
                .where(SubscriptionPlan.is_active, SubscriptionPlan.is_public)
                .order_by(SubscriptionPlan.sort_order, SubscriptionPlan.name)
            )
        )
        .scalars()
        .all()
    )
    prices = (
        (
            await ctx.session.execute(
                select(PlanPrice).where(PlanPrice.is_active, PlanPrice.currency.in_(currencies))
            )
        )
        .scalars()
        .all()
    )
    out = []
    for plan in plans:
        mine = [price_out(p) for p in prices if p.plan_id == plan.id]
        if mine:
            mine.sort(key=lambda p: (p.currency, p.interval != "month"))
            out.append(
                PlanOut(
                    id=plan.id,
                    code=plan.code,
                    name=plan.name,
                    description=plan.description,
                    features=plan.features,
                    limits=plan.limits,
                    prices=mine,
                )
            )
    gateways = {p.gateway for p in prices}
    ready = bool(gateways) and all(get_gateway(g).configured() for g in gateways)
    return PlansOut(currencies=list(currencies), plans=out, payments_ready=ready)


# ---- billing details ----------------------------------------------------------------------


def profile_out(p: BillingProfile) -> ProfileOut:
    return ProfileOut(
        legal_name=p.legal_name,
        email=p.email,
        address_line1=p.address_line1,
        address_line2=p.address_line2,
        city=p.city,
        state=p.state,
        state_code=p.state_code,
        postal_code=p.postal_code,
        country=p.country,
        gstin=p.gstin,
        tax_id=p.tax_id,
        updated_at=p.updated_at,
    )


class StateOut(BaseModel):
    code: str
    name: str


@router.get("/states", response_model=list[StateOut])
async def list_states(ctx: Reader) -> list[StateOut]:
    """Indian states and union territories with their GST codes, for the billing form."""
    return [StateOut(code=c, name=n) for c, n in sorted(gst.STATES.items(), key=lambda x: x[1])]


@router.get("/profile", response_model=ProfileOut | None)
async def get_profile(ctx: Reader) -> ProfileOut | None:
    p = await service.get_profile(ctx.session, ctx.tenant_id)
    return profile_out(p) if p else None


@router.put("/profile", response_model=ProfileOut)
async def put_profile(body: ProfileIn, ctx: Manager, meta: Meta) -> ProfileOut:
    country = ctx.tenant.country
    gstin = body.gstin.upper() if body.gstin else None
    state_code = body.state_code
    if country == "IN":
        if gstin:
            if not gst.valid_gstin(gstin):
                raise ValidationFailed(
                    "That GSTIN isn't valid. Check it and try again.", code="invalid_gstin"
                )
            if state_code and state_code != gstin[:2]:
                raise ValidationFailed(
                    "The state doesn't match the GSTIN's state code.", code="gstin_state_mismatch"
                )
            state_code = gstin[:2]
        if state_code and state_code not in gst.STATES:
            raise ValidationFailed("Choose a valid state.", code="invalid_state")
    else:
        gstin, state_code = None, None
    profile = await service.get_profile(ctx.session, ctx.tenant_id)
    if profile is None:
        profile = BillingProfile(tenant_id=ctx.tenant_id, legal_name=body.legal_name, country=country)
        ctx.session.add(profile)
    profile.legal_name = body.legal_name.strip()
    profile.email = str(body.email) if body.email else None
    profile.address_line1 = body.address_line1
    profile.address_line2 = body.address_line2
    profile.city = body.city
    profile.state_code = state_code
    profile.state = gst.STATES.get(state_code or "") or body.state
    profile.postal_code = body.postal_code
    profile.country = country
    profile.gstin = gstin
    profile.tax_id = body.tax_id if country != "IN" else None
    profile.updated_by_user_id = ctx.user.id
    await ctx.session.flush()
    await ctx.session.refresh(profile)
    await record(
        ctx.session,
        "billing.profile_updated",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="billing_profile",
        target_id=profile.id,
        details={"gstin": gstin, "state_code": state_code},
    )
    return profile_out(profile)


# ---- checkout -------------------------------------------------------------------------------


@router.post("/checkout", response_model=CheckoutOut, status_code=201)
async def start_checkout(body: CheckoutIn, ctx: Manager, meta: Meta) -> CheckoutOut:
    result = await service.start_checkout(
        ctx.session,
        tenant_id=ctx.tenant_id,
        country=ctx.tenant.country,
        user_id=ctx.user.id,
        email=ctx.user.email,
        name=ctx.user.full_name,
        price_id=body.price_id,
        extra_seats=body.extra_seats,
    )
    await _audit(
        ctx, meta, "billing.checkout_started", {"price_id": str(body.price_id), "seats": body.extra_seats}
    )
    return CheckoutOut(
        checkout_id=result.checkout.id,
        gateway=result.gateway,
        razorpay=result.client if result.gateway == "razorpay" else None,
        stripe_url=result.client.get("url") if result.gateway == "stripe" else None,
    )


@router.post("/checkout/razorpay/verify", response_model=SubscriptionOut)
async def verify_razorpay(body: RazorpayVerifyIn, ctx: Manager) -> SubscriptionOut:
    """Razorpay Checkout's success callback. The webhook is the source of truth; this
    only lets the page show the new plan without waiting for it."""
    gateway = Razorpay()
    if not gateway.verify_checkout(
        body.razorpay_payment_id, body.razorpay_subscription_id, body.razorpay_signature
    ):
        raise Unauthorized("The payment confirmation could not be verified.", code="invalid_signature")
    gsub = await gateway.fetch(body.razorpay_subscription_id)
    await service.activate(
        ctx.session, ctx.tenant_id, gateway="razorpay", checkout_ref=body.razorpay_subscription_id, gsub=gsub
    )
    return await subscription_out(ctx)


@router.post("/checkout/stripe/confirm", response_model=SubscriptionOut)
async def confirm_stripe(body: StripeConfirmIn, ctx: Manager) -> SubscriptionOut:
    gateway = Stripe()
    session = await gateway.checkout_session(body.session_id)
    if str((session.get("metadata") or {}).get("tenant_id")) != str(ctx.tenant_id):
        raise NotFound("Checkout not found.", code="checkout_not_found")
    if session.get("status") == "complete" and session.get("subscription"):
        gsub = await gateway.fetch(str(session["subscription"]))
        await service.activate(
            ctx.session, ctx.tenant_id, gateway="stripe", checkout_ref=body.session_id, gsub=gsub
        )
    return await subscription_out(ctx)


# ---- changes ----------------------------------------------------------------------------------


@router.post("/subscription/change", response_model=ChangeOut)
async def change_plan(body: ChangeIn, ctx: Manager, meta: Meta) -> ChangeOut:
    _, immediate = await service.change_plan(ctx.session, ctx.tenant_id, body.price_id)
    await _audit(ctx, meta, "billing.plan_changed", {"price_id": str(body.price_id), "immediate": immediate})
    return ChangeOut(immediate=immediate, subscription=await subscription_out(ctx))


@router.delete("/subscription/pending-change", response_model=SubscriptionOut)
async def undo_change(ctx: Manager, meta: Meta) -> SubscriptionOut:
    await service.undo_pending_change(ctx.session, ctx.tenant_id)
    await _audit(ctx, meta, "billing.plan_change_undone")
    return await subscription_out(ctx)


@router.put("/subscription/seats", response_model=SubscriptionOut)
async def set_seats(body: SeatsIn, ctx: Manager, meta: Meta) -> SubscriptionOut:
    sub = await service.current_subscription(ctx.session, ctx.tenant_id)
    before = sub.extra_seats if sub else 0
    await service.set_seats(ctx.session, ctx.tenant_id, body.extra_seats)
    await _audit(ctx, meta, "billing.seats_changed", {"from": before, "to": body.extra_seats})
    return await subscription_out(ctx)


@router.post("/subscription/cancel", response_model=SubscriptionOut)
async def cancel(ctx: Manager, meta: Meta) -> SubscriptionOut:
    await service.cancel(ctx.session, ctx.tenant_id)
    await _audit(ctx, meta, "billing.canceled")
    return await subscription_out(ctx)


@router.post("/subscription/resume", response_model=SubscriptionOut)
async def resume(ctx: Manager, meta: Meta) -> SubscriptionOut:
    await service.resume(ctx.session, ctx.tenant_id)
    await _audit(ctx, meta, "billing.resumed")
    return await subscription_out(ctx)


# ---- invoices and payments -------------------------------------------------------------------


@router.get("/invoices", response_model=list[InvoiceSummary])
async def list_invoices(ctx: Reader, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[InvoiceSummary]:
    rows = (
        await ctx.session.execute(
            select(Invoice)
            .where(Invoice.tenant_id == ctx.tenant_id)
            .order_by(Invoice.issued_at.desc())
            .limit(limit)
        )
    ).scalars()
    return [InvoiceSummary.model_validate(i, from_attributes=True) for i in rows]


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(invoice_id: uuid.UUID, ctx: Reader) -> InvoiceOut:
    inv = await ctx.session.get(Invoice, invoice_id)
    if inv is None or inv.tenant_id != ctx.tenant_id:
        raise NotFound("Invoice not found.", code="invoice_not_found")
    return InvoiceOut.model_validate(inv, from_attributes=True)


@router.get("/transactions", response_model=list[TransactionOut])
async def list_transactions(
    ctx: Reader, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[TransactionOut]:
    rows = (
        await ctx.session.execute(
            select(Transaction)
            .where(Transaction.tenant_id == ctx.tenant_id)
            .order_by(Transaction.occurred_at.desc())
            .limit(limit)
        )
    ).scalars()
    return [TransactionOut.model_validate(t, from_attributes=True) for t in rows]


# ---- gateway webhooks --------------------------------------------------------------------------


async def _receive(gateway_name: Literal["razorpay", "stripe"], request: Request) -> Response:
    gateway = get_gateway(gateway_name)
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise AppError("Payload too large.", code="payload_too_large")
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not gateway.verify_webhook(body, headers):
        log.warning("billing_webhook_bad_signature", gateway=gateway_name)
        raise Unauthorized("Invalid signature.", code="invalid_signature")
    stored = await events.ingest(gateway_name, body, headers)
    if stored is not None:
        await dispatch(BILLING_EVENT, stored[0], stored[1])
    return Response(status_code=200)


@webhooks.post("/razorpay", include_in_schema=False)
async def razorpay_webhook(request: Request) -> Response:
    return await _receive("razorpay", request)


@webhooks.post("/stripe", include_in_schema=False)
async def stripe_webhook(request: Request) -> Response:
    return await _receive("stripe", request)


__all__ = ["Subscription", "router", "webhooks"]
