from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import Requires, TenantContext
from app.modules.billing.service import effective_plan
from app.modules.rbac.permissions import Permission
from app.modules.tenants.models import TenantMembership

router = APIRouter(prefix="/billing", tags=["billing"])


class SubscriptionOut(BaseModel):
    status: str | None
    plan_code: str | None
    plan_name: str | None
    limits: dict[str, Any]
    trial_ends_at: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    seats_used: int
    seats_included: int


@router.get("/subscription", response_model=SubscriptionOut)
async def get_subscription(
    ctx: Annotated[TenantContext, Requires(Permission.BILLING_READ)],
) -> SubscriptionOut:
    """Current plan, limits and seat usage. Payments and invoices arrive in Phase 7."""
    eff = await effective_plan(ctx.session, ctx.tenant_id)
    seats_used = (
        await ctx.session.scalar(
            select(func.count()).where(
                TenantMembership.tenant_id == ctx.tenant_id, TenantMembership.status == "active"
            )
        )
        or 0
    )
    sub = eff.subscription
    return SubscriptionOut(
        status=sub.status if sub else None,
        plan_code=eff.plan.code if eff.plan else None,
        plan_name=eff.plan.name if eff.plan else None,
        limits=eff.plan.limits if eff.plan else {},
        trial_ends_at=sub.trial_ends_at if sub else None,
        current_period_end=sub.current_period_end if sub else None,
        cancel_at_period_end=sub.cancel_at_period_end if sub else False,
        seats_used=seats_used,
        seats_included=eff.limit("max_users", 0),
    )
