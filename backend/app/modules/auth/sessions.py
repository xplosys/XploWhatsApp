"""Issuing and rotating login sessions (access JWT + refresh token family)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Unauthorized
from app.core.ids import uuid7
from app.core.security.jwt import encode_token
from app.core.security.tokens import generate_token, hash_token
from app.db.session import app_transaction
from app.modules.audit.service import RequestMeta
from app.modules.auth.models import RefreshToken, User
from app.modules.auth.schemas import LoginSessionOut, MembershipSummary, SessionOut, UserOut
from app.modules.tenants.models import TenantRole


@dataclass
class IssuedSession:
    body: SessionOut
    refresh_token: str


def utcnow() -> datetime:
    return datetime.now(UTC)


async def list_memberships(session: AsyncSession, user_id: uuid.UUID) -> list[MembershipSummary]:
    rows = (
        await session.execute(text("SELECT * FROM app_user_memberships(:uid)"), {"uid": user_id})
    ).mappings()
    return [
        MembershipSummary(
            tenant_id=r["tenant_id"],
            tenant_name=r["tenant_name"],
            tenant_slug=r["tenant_slug"],
            role=TenantRole(r["role"]),
            status=r["status"],
        )
        for r in rows
    ]


def pick_tenant(memberships: list[MembershipSummary], preferred: uuid.UUID | None) -> uuid.UUID | None:
    active = [m for m in memberships if m.status == "active"]
    for m in active:
        if m.tenant_id == preferred:
            return m.tenant_id
    return active[0].tenant_id if active else None


async def issue_session(
    session: AsyncSession,
    user: User,
    tenant_id: uuid.UUID | None,
    *,
    mfa_verified: bool,
    meta: RequestMeta | None,
    family_id: uuid.UUID | None = None,
    memberships: list[MembershipSummary] | None = None,
) -> IssuedSession:
    settings = get_settings()
    family_id = family_id or uuid7()
    refresh_plain = generate_token(32)
    record = RefreshToken(
        id=uuid7(),
        user_id=user.id,
        tenant_id=tenant_id,
        family_id=family_id,
        token_hash=hash_token(refresh_plain),
        mfa_verified=mfa_verified,
        expires_at=utcnow() + timedelta(seconds=settings.refresh_token_ttl_seconds),
        created_ip=meta.ip if meta and meta.ip and meta.ip != "unknown" else None,
        user_agent=meta.user_agent if meta else None,
    )
    session.add(record)
    access = encode_token(
        {
            "sub": str(user.id),
            "tid": str(tenant_id) if tenant_id else None,
            "sid": str(family_id),
            "mfa": mfa_verified,
        },
        ttl_seconds=settings.access_token_ttl_seconds,
    )
    if memberships is None:
        memberships = await list_memberships(session, user.id)
    if tenant_id is not None:
        user.last_tenant_id = tenant_id
    await session.flush()
    return IssuedSession(
        body=SessionOut(
            access_token=access,
            expires_in=settings.access_token_ttl_seconds,
            user=UserOut.model_validate(user),
            active_tenant_id=tenant_id,
            memberships=memberships,
        ),
        refresh_token=refresh_plain,
    )


async def revoke_family(session: AsyncSession, family_id: uuid.UUID, reason: str) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )


async def revoke_all_for_user(
    session: AsyncSession, user_id: uuid.UUID, reason: str, *, keep_family: uuid.UUID | None = None
) -> int:
    stmt = update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
    if keep_family is not None:
        stmt = stmt.where(RefreshToken.family_id != keep_family)
    result = await session.execute(stmt.values(revoked_at=utcnow(), revoked_reason=reason))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def list_login_sessions(
    session: AsyncSession, user_id: uuid.UUID, current: uuid.UUID | None
) -> list[LoginSessionOut]:
    """Signed-in devices: one per refresh-token family that still has a live token. The
    first token of a family marks the sign-in, the live one the last refresh."""
    now = utcnow()
    rows = (
        (
            await session.execute(
                select(RefreshToken).where(RefreshToken.user_id == user_id).order_by(RefreshToken.created_at)
            )
        )
        .scalars()
        .all()
    )
    first: dict[uuid.UUID, datetime] = {}
    live: dict[uuid.UUID, RefreshToken] = {}
    for row in rows:
        first.setdefault(row.family_id, row.created_at)
        if row.revoked_at is None and row.expires_at > now:
            live[row.family_id] = row
    out = [
        LoginSessionOut(
            id=family,
            signed_in_at=first[family],
            last_active_at=token.created_at,
            ip=str(token.created_ip) if token.created_ip else None,
            user_agent=token.user_agent,
            current=family == current,
        )
        for family, token in live.items()
    ]
    return sorted(out, key=lambda s: (not s.current, -s.last_active_at.timestamp()))


class RefreshReuseDetected(Unauthorized):
    code = "refresh_token_reused"
    message = "This session was signed out for security. Please sign in again."


async def consume_refresh_token(session: AsyncSession, refresh_plain: str) -> RefreshToken:
    """Validate a refresh token and mark it rotated. Reuse of an already-rotated
    token revokes the whole family (likely theft)."""
    record = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_plain)).with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        raise Unauthorized("Session not found. Please sign in again.", code="invalid_refresh_token")
    if record.revoked_at is not None:
        if record.revoked_reason == "rotated":
            # Commit the family revocation independently: the caller's transaction
            # is rolled back when we raise.
            async with app_transaction() as side:
                await revoke_family(side, record.family_id, "reuse_detected")
            raise RefreshReuseDetected()
        raise Unauthorized("Session has ended. Please sign in again.", code="invalid_refresh_token")
    if record.expires_at <= utcnow():
        raise Unauthorized("Session has expired. Please sign in again.", code="refresh_token_expired")
    record.revoked_at = utcnow()
    record.revoked_reason = "rotated"
    return record


async def rotate_session(session: AsyncSession, refresh_plain: str, meta: RequestMeta) -> IssuedSession:
    """Exchange a refresh token for a new access token and refresh token in the same family."""
    record = await consume_refresh_token(session, refresh_plain)
    user = await session.get(User, record.user_id)
    if user is None or not user.is_active:
        raise Unauthorized("Account is not active.", code="account_inactive")
    memberships = await list_memberships(session, user.id)
    tenant_id = record.tenant_id
    if tenant_id not in {m.tenant_id for m in memberships if m.status == "active"}:
        # Membership was removed since the last refresh: fall back to another business, or none.
        tenant_id = pick_tenant(memberships, None)
    issued = await issue_session(
        session,
        user,
        tenant_id,
        mfa_verified=record.mfa_verified,
        meta=meta,
        family_id=record.family_id,
        memberships=memberships,
    )
    record.replaced_by_id = (
        await session.execute(
            select(RefreshToken.id).where(
                RefreshToken.family_id == record.family_id, RefreshToken.revoked_at.is_(None)
            )
        )
    ).scalar_one()
    return issued


async def end_session(session: AsyncSession, refresh_plain: str) -> uuid.UUID | None:
    """Revoke the family of this refresh token. Returns the user id, if found."""
    record = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_plain))
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    await revoke_family(session, record.family_id, "logout")
    return record.user_id
