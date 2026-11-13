"""Tenant profile, members and invitations. Every query runs under RLS for the
caller's tenant; explicit tenant filters are kept as a second layer."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import TenantContext
from app.api.pagination import Page, PageParams
from app.core.config import get_settings
from app.core.errors import Conflict, Forbidden, NotFound
from app.core.security.tokens import generate_token, hash_token
from app.modules.audit import service as audit
from app.modules.audit.service import RequestMeta
from app.modules.auth.models import User
from app.modules.auth.sessions import utcnow
from app.modules.billing.service import effective_plan
from app.modules.notifications import email as mail
from app.modules.rbac.permissions import can_assign_role, can_manage_member
from app.modules.tenants.models import Tenant, TenantInvitation, TenantMembership, TenantRole
from app.modules.tenants.schemas import InvitationCreate, MemberOut, MemberUpdate, TenantUpdate


async def update_tenant(ctx: TenantContext, data: TenantUpdate, meta: RequestMeta) -> Tenant:
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        setattr(ctx.tenant, key, value)
    if changes:
        await audit.record(
            ctx.session,
            "tenant.updated",
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="tenant",
            target_id=ctx.tenant_id,
            details={"fields": sorted(changes)},
        )
    await ctx.session.flush()
    await ctx.session.refresh(ctx.tenant)
    return ctx.tenant


def _member_out(m: TenantMembership, u: User) -> MemberOut:
    return MemberOut(
        id=m.id,
        user_id=u.id,
        email=u.email,
        full_name=u.full_name,
        role=TenantRole(m.role),
        status=m.status,
        created_at=m.created_at,
    )


async def list_members(ctx: TenantContext, page: PageParams) -> Page[MemberOut]:
    base = (
        select(TenantMembership, User)
        .join(User, User.id == TenantMembership.user_id)
        .where(TenantMembership.tenant_id == ctx.tenant_id)
    )
    total = await ctx.session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = (
        await ctx.session.execute(
            base.order_by(TenantMembership.created_at).limit(page.limit).offset(page.offset)
        )
    ).all()
    return Page(items=[_member_out(m, u) for m, u in rows], total=total, limit=page.limit, offset=page.offset)


async def _get_membership(
    session: AsyncSession, tenant_id: uuid.UUID, membership_id: uuid.UUID
) -> TenantMembership:
    m = (
        await session.execute(
            select(TenantMembership)
            .where(TenantMembership.id == membership_id, TenantMembership.tenant_id == tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if m is None:
        raise NotFound("Member not found.", code="member_not_found")
    return m


async def _owner_count(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count()).where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.role == TenantRole.OWNER.value,
                TenantMembership.status == "active",
            )
        )
        or 0
    )


async def update_member(
    ctx: TenantContext, membership_id: uuid.UUID, data: MemberUpdate, meta: RequestMeta
) -> MemberOut:
    m = await _get_membership(ctx.session, ctx.tenant_id, membership_id)
    if m.user_id == ctx.user.id:
        raise Forbidden("You cannot change your own role or status.", code="cannot_modify_self")
    if not can_manage_member(ctx.role, TenantRole(m.role)):
        raise Forbidden("You cannot manage a member with this role.", code="role_hierarchy")
    details: dict[str, str] = {}
    if data.role is not None and data.role.value != m.role:
        if not can_assign_role(ctx.role, data.role):
            raise Forbidden("You cannot assign this role.", code="role_hierarchy")
        details["role"] = f"{m.role}->{data.role.value}"
        m.role = data.role.value
    if data.status is not None and data.status != m.status:
        details["status"] = f"{m.status}->{data.status}"
        m.status = data.status
    if details:
        await audit.record(
            ctx.session,
            "member.updated",
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="membership",
            target_id=m.id,
            details=details,
        )
    await ctx.session.flush()
    user = await ctx.session.get(User, m.user_id)
    assert user is not None
    return _member_out(m, user)


async def remove_member(ctx: TenantContext, membership_id: uuid.UUID, meta: RequestMeta) -> None:
    m = await _get_membership(ctx.session, ctx.tenant_id, membership_id)
    if m.user_id == ctx.user.id:
        raise Forbidden("You cannot remove yourself.", code="cannot_modify_self")
    if not can_manage_member(ctx.role, TenantRole(m.role)):
        raise Forbidden("You cannot remove a member with this role.", code="role_hierarchy")
    if m.role == TenantRole.OWNER.value and await _owner_count(ctx.session, ctx.tenant_id) <= 1:
        raise Conflict("A business must keep at least one owner.", code="last_owner")
    await ctx.session.delete(m)
    from app.modules.conversations.inbox import unassign_member

    await unassign_member(ctx.session, ctx.tenant_id, m.user_id)
    await audit.record(
        ctx.session,
        "member.removed",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="user",
        target_id=m.user_id,
        details={"role": m.role},
    )


async def list_invitations(ctx: TenantContext, page: PageParams) -> Page[TenantInvitation]:
    base = select(TenantInvitation).where(
        TenantInvitation.tenant_id == ctx.tenant_id,
        TenantInvitation.accepted_at.is_(None),
        TenantInvitation.revoked_at.is_(None),
        TenantInvitation.expires_at > utcnow(),
    )
    total = await ctx.session.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = (
        (
            await ctx.session.execute(
                base.order_by(TenantInvitation.created_at.desc()).limit(page.limit).offset(page.offset)
            )
        )
        .scalars()
        .all()
    )
    return Page(items=list(items), total=total, limit=page.limit, offset=page.offset)


async def _seats_in_use(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    members = (
        await session.scalar(
            select(func.count()).where(
                TenantMembership.tenant_id == tenant_id, TenantMembership.status == "active"
            )
        )
        or 0
    )
    pending = (
        await session.scalar(
            select(func.count()).where(
                TenantInvitation.tenant_id == tenant_id,
                TenantInvitation.accepted_at.is_(None),
                TenantInvitation.revoked_at.is_(None),
                TenantInvitation.expires_at > utcnow(),
            )
        )
        or 0
    )
    return members + pending


async def create_invitation(
    ctx: TenantContext, data: InvitationCreate, meta: RequestMeta
) -> TenantInvitation:
    if not can_assign_role(ctx.role, data.role):
        raise Forbidden("You cannot invite someone with this role.", code="role_hierarchy")
    plan = await effective_plan(ctx.session, ctx.tenant_id)
    if plan.plan is None:
        raise Forbidden("This business has no active subscription.", code="no_active_subscription")
    max_users = plan.limit("max_users", 1)
    if await _seats_in_use(ctx.session, ctx.tenant_id) >= max_users:
        raise Conflict(
            f"Your plan allows {max_users} team members. Upgrade or remove a member first.",
            code="seat_limit_reached",
            details={"max_users": max_users},
        )
    already_member = await ctx.session.scalar(
        select(func.count())
        .select_from(TenantMembership)
        .join(User, User.id == TenantMembership.user_id)
        .where(TenantMembership.tenant_id == ctx.tenant_id, User.email == data.email)
    )
    if already_member:
        raise Conflict("This person is already a member.", code="already_member")
    # Re-inviting replaces any pending invitation for the same email.
    pending = (
        (
            await ctx.session.execute(
                select(TenantInvitation).where(
                    TenantInvitation.tenant_id == ctx.tenant_id,
                    TenantInvitation.email == data.email,
                    TenantInvitation.accepted_at.is_(None),
                    TenantInvitation.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for inv in pending:
        inv.revoked_at = utcnow()
    await ctx.session.flush()
    plain = generate_token(32)
    settings = get_settings()
    invitation = TenantInvitation(
        tenant_id=ctx.tenant_id,
        email=data.email,
        role=data.role.value,
        token_hash=hash_token(plain),
        invited_by_user_id=ctx.user.id,
        expires_at=utcnow() + timedelta(seconds=settings.invitation_ttl_seconds),
    )
    ctx.session.add(invitation)
    await audit.record(
        ctx.session,
        "invitation.created",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="invitation",
        target_id=invitation.id,
        details={"email": data.email, "role": data.role.value},
    )
    await ctx.session.flush()
    await ctx.session.refresh(invitation)
    mail.send_email(
        mail.invitation_email(
            data.email,
            ctx.tenant.name,
            ctx.user.full_name,
            f"{settings.frontend_base_url}/accept-invite?token={plain}",
        )
    )
    return invitation


async def revoke_invitation(ctx: TenantContext, invitation_id: uuid.UUID, meta: RequestMeta) -> None:
    inv = (
        await ctx.session.execute(
            select(TenantInvitation).where(
                TenantInvitation.id == invitation_id, TenantInvitation.tenant_id == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if inv is None or inv.accepted_at is not None:
        raise NotFound("Invitation not found.", code="invitation_not_found")
    if inv.revoked_at is None:
        inv.revoked_at = utcnow()
        await audit.record(
            ctx.session,
            "invitation.revoked",
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="invitation",
            target_id=inv.id,
        )
