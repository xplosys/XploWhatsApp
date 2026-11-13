"""Super Admin console API. Uses the BYPASSRLS admin role, so every endpoint
requires a Super Admin who signed in with MFA, and every write is audited."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Row, Select, func, or_, select

from app.api.deps import Meta, SuperAdmin
from app.api.pagination import Page, PageParams, page_params
from app.core.errors import NotFound
from app.db.session import admin_transaction
from app.modules.audit import service as audit
from app.modules.billing.models import Subscription, SubscriptionPlan
from app.modules.tenants.models import Tenant, TenantMembership

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminTenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    country: str
    created_at: datetime
    member_count: int
    plan_code: str | None
    subscription_status: str | None


def _tenant_query() -> Select[Tenant, int, str | None, str | None]:
    members = (
        select(func.count())
        .where(TenantMembership.tenant_id == Tenant.id)
        .correlate(Tenant)
        .scalar_subquery()
    )
    return (
        select(Tenant, members.label("member_count"), SubscriptionPlan.code, Subscription.status)
        .outerjoin(
            Subscription,
            (Subscription.tenant_id == Tenant.id)
            & Subscription.status.in_(("trialing", "active", "past_due")),
        )
        .outerjoin(SubscriptionPlan, SubscriptionPlan.id == Subscription.plan_id)
        .where(Tenant.deleted_at.is_(None))
    )


def _out(row: Row[Tenant, int, str | None, str | None]) -> AdminTenantOut:
    t, count, plan_code, sub_status = row
    return AdminTenantOut(
        id=t.id,
        name=t.name,
        slug=t.slug,
        status=t.status,
        country=t.country,
        created_at=t.created_at,
        member_count=count,
        plan_code=plan_code,
        subscription_status=sub_status,
    )


@router.get("/tenants", response_model=Page[AdminTenantOut])
async def list_tenants(
    _: SuperAdmin,
    page: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[AdminTenantOut]:
    async with admin_transaction() as session:
        query = _tenant_query()
        if q:
            like = f"%{q}%"
            query = query.where(or_(Tenant.name.ilike(like), Tenant.slug.ilike(like)))
        total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = (
            await session.execute(
                query.order_by(Tenant.created_at.desc()).limit(page.limit).offset(page.offset)
            )
        ).all()
        return Page(items=[_out(r) for r in rows], total=total, limit=page.limit, offset=page.offset)


async def _set_status(tenant_id: uuid.UUID, new_status: str, ctx: SuperAdmin, meta: Meta) -> AdminTenantOut:
    async with admin_transaction() as session:
        tenant = await session.get(Tenant, tenant_id, with_for_update=True)
        if tenant is None or tenant.deleted_at is not None:
            raise NotFound("Tenant not found.")
        tenant.status = new_status
        await audit.record(
            session,
            "admin.tenant_suspended" if new_status == "suspended" else "admin.tenant_reactivated",
            tenant_id=tenant_id,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="tenant",
            target_id=tenant_id,
        )
        await session.flush()
        row = (await session.execute(_tenant_query().where(Tenant.id == tenant_id))).one()
        return _out(row)


@router.post("/tenants/{tenant_id}/suspend", response_model=AdminTenantOut)
async def suspend_tenant(tenant_id: uuid.UUID, ctx: SuperAdmin, meta: Meta) -> AdminTenantOut:
    return await _set_status(tenant_id, "suspended", ctx, meta)


@router.post("/tenants/{tenant_id}/reactivate", response_model=AdminTenantOut)
async def reactivate_tenant(tenant_id: uuid.UUID, ctx: SuperAdmin, meta: Meta) -> AdminTenantOut:
    return await _set_status(tenant_id, "active", ctx, meta)


class DeletedTenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    deleted_at: datetime
    purge_after: datetime | None


@router.get("/tenants-deleted", response_model=list[DeletedTenantOut])
async def list_deleted_tenants(_: SuperAdmin) -> list[DeletedTenantOut]:
    """Businesses their owners deleted that are still inside the grace period."""
    async with admin_transaction() as session:
        rows = (
            await session.execute(
                select(Tenant)
                .where(Tenant.deleted_at.is_not(None), Tenant.purged_at.is_(None))
                .order_by(Tenant.purge_after)
                .limit(200)
            )
        ).scalars()
        return [
            DeletedTenantOut(
                id=t.id, name=t.name, slug=t.slug, deleted_at=t.deleted_at, purge_after=t.purge_after
            )
            for t in rows
            if t.deleted_at is not None
        ]


@router.post("/tenants/{tenant_id}/restore", response_model=AdminTenantOut)
async def restore_tenant(tenant_id: uuid.UUID, ctx: SuperAdmin, meta: Meta) -> AdminTenantOut:
    """Undo a business deletion during the grace period (the owner asked support)."""
    from app.modules.privacy.service import restore_business

    async with admin_transaction() as session:
        tenant = await session.get(Tenant, tenant_id, with_for_update=True)
        if tenant is None:
            raise NotFound("Tenant not found.")
        await restore_business(session, tenant)
        await audit.record(
            session,
            "admin.tenant_restored",
            tenant_id=tenant_id,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="tenant",
            target_id=tenant_id,
        )
        await session.flush()
        row = (await session.execute(_tenant_query().where(Tenant.id == tenant_id))).one()
        return _out(row)
