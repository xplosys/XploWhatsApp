"""Super Admin: subscription plans, their limits and features, and their prices.

Prices are data. Each one is entered before tax, gets the tax rate that applies to its
currency when it is created, and is created on the gateway (a Razorpay plan, or a Stripe
product and prices) at the same time. A price is never edited: adding a new one for the
same plan, currency and interval retires the old one, and existing subscribers keep the
old price until they change plan.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Meta, SuperAdmin
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.db.session import admin_transaction
from app.modules.audit import service as audit
from app.modules.billing.gateways.base import (
    CURRENCIES,
    gateway_for_currency,
    get_gateway,
    money,
    with_tax,
)
from app.modules.billing.models import PlanPrice, Subscription, SubscriptionPlan
from app.modules.billing.service import LIMIT_LABELS, LIVE_STATUSES, TRIAL_PLAN_CODE, tax_rate_for

router = APIRouter(prefix="/admin/billing", tags=["admin"])

LIMIT_KEYS = (*LIMIT_LABELS, "platform_ai_keys")


class AdminPriceOut(BaseModel):
    id: uuid.UUID
    currency: str
    interval: str
    amount: Decimal
    seat_amount: Decimal | None
    tax_rate: Decimal
    amount_with_tax: Decimal
    gateway: str
    gateway_plan_id: str | None
    gateway_seat_price_id: str | None
    is_active: bool
    created_at: datetime


class AdminPlanOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    limits: dict[str, Any]
    features: list[str]
    trial_days: int
    is_public: bool
    is_active: bool
    sort_order: int
    subscribers: int
    prices: list[AdminPriceOut]


def _check_limits(value: dict[str, int] | None) -> dict[str, int] | None:
    if value is None:
        return None
    unknown = sorted(set(value) - set(LIMIT_KEYS))
    if unknown:
        raise ValueError(f"Unknown limits: {', '.join(unknown)}")
    if any(v < 0 for v in value.values()):
        raise ValueError("Limits can't be negative.")
    return value


class PlanCreate(BaseModel):
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    limits: dict[str, int]
    features: list[str] = Field(default_factory=list, max_length=12)
    is_public: bool = True
    sort_order: int = 0

    _limits = field_validator("limits")(_check_limits)


class PlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    limits: dict[str, int] | None = None
    features: list[str] | None = Field(default=None, max_length=12)
    trial_days: int | None = Field(default=None, ge=1, le=90)
    is_public: bool | None = None
    is_active: bool | None = None
    sort_order: int | None = None

    _limits = field_validator("limits")(_check_limits)


class PriceCreate(BaseModel):
    currency: str
    interval: str = Field(pattern=r"^(month|year)$")
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    seat_amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        value = value.upper()
        if value not in CURRENCIES:
            raise ValueError(f"Currency must be one of {', '.join(CURRENCIES)}.")
        return value


def _price_out(p: PlanPrice) -> AdminPriceOut:
    return AdminPriceOut(
        id=p.id,
        currency=p.currency,
        interval=p.interval,
        amount=p.amount,
        seat_amount=p.seat_amount,
        tax_rate=p.tax_rate,
        amount_with_tax=with_tax(p.amount, p.tax_rate),
        gateway=p.gateway,
        gateway_plan_id=p.gateway_plan_id,
        gateway_seat_price_id=p.gateway_seat_price_id,
        is_active=p.is_active,
        created_at=p.created_at,
    )


async def _plan_out(session: AsyncSession, plan: SubscriptionPlan) -> AdminPlanOut:
    prices = (
        await session.execute(
            select(PlanPrice)
            .where(PlanPrice.plan_id == plan.id)
            .order_by(
                PlanPrice.is_active.desc(),
                PlanPrice.currency,
                PlanPrice.interval,
                PlanPrice.created_at.desc(),
            )
        )
    ).scalars()
    subscribers = await session.scalar(
        select(func.count()).where(Subscription.plan_id == plan.id, Subscription.status.in_(LIVE_STATUSES))
    )
    return AdminPlanOut(
        id=plan.id,
        code=plan.code,
        name=plan.name,
        description=plan.description,
        limits=plan.limits,
        features=plan.features,
        trial_days=plan.trial_days,
        is_public=plan.is_public,
        is_active=plan.is_active,
        sort_order=plan.sort_order,
        subscribers=int(subscribers or 0),
        prices=[_price_out(p) for p in prices],
    )


async def _plan(session: AsyncSession, plan_id: uuid.UUID) -> SubscriptionPlan:
    plan = await session.get(SubscriptionPlan, plan_id, with_for_update=True)
    if plan is None:
        raise NotFound("Plan not found.")
    return plan


@router.get("/plans", response_model=list[AdminPlanOut])
async def list_plans(ctx: SuperAdmin) -> list[AdminPlanOut]:
    async with admin_transaction() as session:
        plans = (
            await session.execute(
                select(SubscriptionPlan).order_by(SubscriptionPlan.sort_order, SubscriptionPlan.code)
            )
        ).scalars()
        return [await _plan_out(session, p) for p in plans]


@router.post("/plans", response_model=AdminPlanOut, status_code=201)
async def create_plan(body: PlanCreate, ctx: SuperAdmin, meta: Meta) -> AdminPlanOut:
    async with admin_transaction() as session:
        if await session.scalar(select(SubscriptionPlan.id).where(SubscriptionPlan.code == body.code)):
            raise Conflict("A plan with this code already exists.", code="plan_exists")
        plan = SubscriptionPlan(
            code=body.code,
            name=body.name,
            description=body.description,
            limits=body.limits,
            features=[f.strip() for f in body.features if f.strip()],
            trial_days=0,
            is_public=body.is_public,
            is_active=True,
            sort_order=body.sort_order,
        )
        session.add(plan)
        await session.flush()
        await audit.record(
            session,
            "admin.plan_created",
            tenant_id=None,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="subscription_plan",
            target_id=plan.id,
            details={"code": plan.code, "limits": plan.limits},
        )
        return await _plan_out(session, plan)


@router.patch("/plans/{plan_id}", response_model=AdminPlanOut)
async def update_plan(plan_id: uuid.UUID, body: PlanUpdate, ctx: SuperAdmin, meta: Meta) -> AdminPlanOut:
    """Limit changes reach every subscriber of the plan straight away."""
    changes = body.model_dump(exclude_unset=True)
    async with admin_transaction() as session:
        plan = await _plan(session, plan_id)
        if "trial_days" in changes and plan.code != TRIAL_PLAN_CODE:
            raise ValidationFailed("Only the trial plan has trial days.", code="not_trial_plan")
        if plan.code == TRIAL_PLAN_CODE and changes.get("is_active") is False:
            raise ValidationFailed("The trial plan can't be turned off; new businesses start on it.")
        if "features" in changes and changes["features"] is not None:
            changes["features"] = [f.strip() for f in changes["features"] if f.strip()]
        if "limits" in changes and changes["limits"] is not None:
            changes["limits"] = {**plan.limits, **changes["limits"]}
        before = {k: getattr(plan, k) for k in changes}
        for key, value in changes.items():
            if value is not None or key == "description":
                setattr(plan, key, value)
        if "limits" in changes:
            plan.version += 1
        await audit.record(
            session,
            "admin.plan_updated",
            tenant_id=None,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="subscription_plan",
            target_id=plan.id,
            details={"before": before, "after": {k: getattr(plan, k) for k in changes}},
        )
        await session.flush()
        return await _plan_out(session, plan)


@router.post("/plans/{plan_id}/prices", response_model=AdminPlanOut, status_code=201)
async def add_price(plan_id: uuid.UUID, body: PriceCreate, ctx: SuperAdmin, meta: Meta) -> AdminPlanOut:
    async with admin_transaction() as session:
        plan = await _plan(session, plan_id)
        if plan.code == TRIAL_PLAN_CODE:
            raise ValidationFailed("The trial plan is free and has no prices.", code="trial_has_no_price")
        gateway_name = gateway_for_currency(body.currency)
        price = PlanPrice(
            id=uuid.uuid4(),
            plan_id=plan.id,
            currency=body.currency,
            interval=body.interval,
            amount=money(body.amount),
            seat_amount=money(body.seat_amount) if body.seat_amount else None,
            tax_rate=tax_rate_for(body.currency),
            gateway=gateway_name,
            is_active=True,
        )
        # The gateway call comes first: if it fails nothing is stored.
        price.gateway_plan_id, price.gateway_seat_price_id = await get_gateway(gateway_name).create_price(
            plan, price
        )
        old = (
            await session.execute(
                select(PlanPrice)
                .where(
                    PlanPrice.plan_id == plan.id,
                    PlanPrice.currency == body.currency,
                    PlanPrice.interval == body.interval,
                    PlanPrice.is_active,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if old is not None:
            old.is_active = False
            await session.flush()
        session.add(price)
        await session.flush()
        await audit.record(
            session,
            "admin.plan_price_added",
            tenant_id=None,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="plan_price",
            target_id=price.id,
            details={
                "plan": plan.code,
                "currency": price.currency,
                "interval": price.interval,
                "amount": str(price.amount),
                "seat_amount": str(price.seat_amount) if price.seat_amount else None,
                "tax_rate": str(price.tax_rate),
                "replaces": str(old.id) if old else None,
            },
        )
        return await _plan_out(session, plan)


@router.delete("/plans/{plan_id}/prices/{price_id}", response_model=AdminPlanOut)
async def retire_price(plan_id: uuid.UUID, price_id: uuid.UUID, ctx: SuperAdmin, meta: Meta) -> AdminPlanOut:
    """Stops new sales at this price. Current subscribers keep paying it."""
    async with admin_transaction() as session:
        plan = await _plan(session, plan_id)
        price = await session.get(PlanPrice, price_id, with_for_update=True)
        if price is None or price.plan_id != plan.id:
            raise NotFound("Price not found.")
        if price.is_active:
            price.is_active = False
            await audit.record(
                session,
                "admin.plan_price_retired",
                tenant_id=None,
                actor_user_id=ctx.user.id,
                meta=meta,
                target_type="plan_price",
                target_id=price.id,
                details={"plan": plan.code, "currency": price.currency, "interval": price.interval},
            )
            await session.flush()
        return await _plan_out(session, plan)
