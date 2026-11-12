"""Phase 1 billing: trial start and plan limits lookup."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.modules.billing.models import Subscription, SubscriptionPlan

TRIAL_PLAN_CODE = "trial"
LIVE_STATUSES = ("trialing", "active", "past_due")


class PlanCatalogueMissing(AppError):
    status_code = 503
    code = "plan_catalogue_missing"
    message = "Plans are not configured yet. Run the seed command."


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
    now = datetime.now(UTC)
    sub = Subscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="trialing",
        trial_ends_at=now + timedelta(days=plan.trial_days),
        current_period_start=now,
        current_period_end=now + timedelta(days=plan.trial_days),
    )
    session.add(sub)
    return sub


@dataclass
class EffectivePlan:
    subscription: Subscription | None
    plan: SubscriptionPlan | None

    def limit(self, key: str, default: int = 0) -> int:
        if self.plan is None:
            return default
        value: Any = self.plan.limits.get(key, default)
        if key == "max_users" and self.subscription is not None:
            value = int(value) + self.subscription.extra_seats
        return int(value)


async def effective_plan(session: AsyncSession, tenant_id: uuid.UUID) -> EffectivePlan:
    """Current live subscription for the tenant. RLS restricts this to the session's tenant."""
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
