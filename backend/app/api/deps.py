"""Request-scoped dependencies: database sessions, the authenticated principal,
tenant context and permission checks.

The tenant comes only from the server-issued access token, and membership and
role are re-read from the database on every request, so a removed member or a
changed role takes effect immediately. Any tenant id in a request body is ignored.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Forbidden, Unauthorized
from app.core.rate_limit import client_ip, hit
from app.core.security.jwt import decode_token
from app.db.session import get_app_sessionmaker, set_tenant_context
from app.modules.audit.service import RequestMeta
from app.modules.auth.models import RefreshToken, User
from app.modules.rbac.permissions import Permission, has_permission
from app.modules.tenants.models import Tenant, TenantMembership, TenantRole


def request_meta(request: Request) -> RequestMeta:
    return RequestMeta(
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
    )


Meta = Annotated[RequestMeta, Depends(request_meta)]


async def db_session() -> AsyncIterator[AsyncSession]:
    """Transaction on the RLS role with no tenant context (tenant tables are invisible)."""
    async with get_app_sessionmaker()() as session, session.begin():
        await set_tenant_context(session, None)
        yield session


DbSession = Annotated[AsyncSession, Depends(db_session)]


@dataclass
class TokenClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID | None
    session_id: uuid.UUID | None
    mfa_verified: bool
    issued_at: datetime


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized()
    return token


def token_claims(request: Request) -> TokenClaims:
    claims = decode_token(_bearer(request), expected_type="access")
    try:
        tid = claims.get("tid")
        sid = claims.get("sid")
        return TokenClaims(
            user_id=uuid.UUID(claims["sub"]),
            tenant_id=uuid.UUID(tid) if tid else None,
            session_id=uuid.UUID(sid) if sid else None,
            mfa_verified=bool(claims.get("mfa", False)),
            issued_at=datetime.fromtimestamp(int(claims["iat"])).astimezone(),
        )
    except (KeyError, ValueError) as exc:
        raise Unauthorized("Invalid token.", code="invalid_token") from exc


Claims = Annotated[TokenClaims, Depends(token_claims)]


async def _load_user(session: AsyncSession, claims: TokenClaims) -> User:
    user = await session.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise Unauthorized("Account is not active.", code="account_inactive")
    if user.password_changed_at and claims.issued_at.timestamp() < int(user.password_changed_at.timestamp()):
        raise Unauthorized("Session expired after a password change.", code="token_revoked")
    # An access token only lives as long as its sign-in: signing out, "sign out other
    # devices", a password change or refresh-token theft end it at once, not when it expires.
    live = None
    if claims.session_id is not None:
        live = await session.scalar(
            select(RefreshToken.id)
            .where(
                RefreshToken.family_id == claims.session_id,
                RefreshToken.user_id == user.id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > datetime.now(UTC),
            )
            .limit(1)
        )
    if live is None:
        raise Unauthorized("This session has ended. Please sign in again.", code="session_revoked")
    return user


@dataclass
class UserContext:
    session: AsyncSession
    user: User
    claims: TokenClaims


async def user_context(claims: Claims) -> AsyncIterator[UserContext]:
    """Authenticated user, with the token's tenant (if any) set as RLS context.
    Every signed-in request also counts towards the person's per-minute limit."""
    await hit(f"api:{claims.user_id}", get_settings().rate_limit_default_per_minute)
    async with get_app_sessionmaker()() as session, session.begin():
        await set_tenant_context(session, claims.tenant_id, claims.user_id)
        user = await _load_user(session, claims)
        yield UserContext(session=session, user=user, claims=claims)


CurrentUser = Annotated[UserContext, Depends(user_context)]


@dataclass
class TenantContext:
    session: AsyncSession
    user: User
    tenant: Tenant
    membership: TenantMembership
    claims: TokenClaims
    role: TenantRole = field(init=False)

    def __post_init__(self) -> None:
        self.role = TenantRole(self.membership.role)

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id

    def can(self, permission: Permission) -> bool:
        return has_permission(self.role, permission)


async def tenant_context(ctx: CurrentUser) -> TenantContext:
    if ctx.claims.tenant_id is None:
        raise Forbidden("Select a business first.", code="no_active_tenant")
    tenant = await ctx.session.get(Tenant, ctx.claims.tenant_id)
    membership = (
        await ctx.session.execute(
            select(TenantMembership).where(
                TenantMembership.tenant_id == ctx.claims.tenant_id,
                TenantMembership.user_id == ctx.user.id,
            )
        )
    ).scalar_one_or_none()
    if tenant is None or tenant.deleted_at is not None or membership is None:
        raise Forbidden("You are not a member of this business.", code="not_a_member")
    if membership.status != "active":
        raise Forbidden("Your access to this business is suspended.", code="membership_suspended")
    if tenant.status != "active":
        raise Forbidden("This business account is suspended.", code="tenant_suspended")
    if tenant.require_mfa and not ctx.claims.mfa_verified:
        raise Forbidden(
            "This business requires two-factor authentication. Turn it on in your account, "
            "then sign in again.",
            code="mfa_required_by_business",
        )
    return TenantContext(
        session=ctx.session, user=ctx.user, tenant=tenant, membership=membership, claims=ctx.claims
    )


CurrentTenant = Annotated[TenantContext, Depends(tenant_context)]


def require_permission(permission: Permission) -> Callable[[TenantContext], Awaitable[TenantContext]]:
    async def dependency(ctx: CurrentTenant) -> TenantContext:
        if not ctx.can(permission):
            raise Forbidden(code="permission_denied", details={"permission": permission.value})
        return ctx

    return dependency


async def super_admin(ctx: CurrentUser) -> UserContext:
    if not ctx.user.is_super_admin:
        raise Forbidden(code="permission_denied")
    if not ctx.user.mfa_enabled:
        raise Forbidden(
            "Enable two-factor authentication to use the admin console.", code="mfa_setup_required"
        )
    if not ctx.claims.mfa_verified:
        raise Forbidden("Sign in again with two-factor authentication.", code="mfa_required")
    return ctx


SuperAdmin = Annotated[UserContext, Depends(super_admin)]


def Requires(permission: Permission) -> Any:
    """Usage: ctx: Annotated[TenantContext, Requires(Permission.MEMBERS_READ)]"""
    return Depends(require_permission(permission))
