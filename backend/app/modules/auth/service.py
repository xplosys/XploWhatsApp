"""Authentication use cases: registration, login (with MFA), email verification,
password reset/change, MFA enrolment and invitation acceptance."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Conflict, Forbidden, NotFound, Unauthorized, ValidationFailed
from app.core.ids import uuid7
from app.core.redis import get_redis
from app.core.security import crypto, totp
from app.core.security.jwt import decode_token, encode_token
from app.core.security.passwords import (
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)
from app.core.security.tokens import generate_token, hash_token
from app.db.session import app_transaction, set_tenant_context
from app.modules.audit import service as audit
from app.modules.audit.service import RequestMeta
from app.modules.auth.models import TokenPurpose, User, UserActionToken
from app.modules.auth.schemas import RegisterRequest
from app.modules.auth.sessions import IssuedSession, issue_session, list_memberships, pick_tenant, utcnow
from app.modules.billing.service import start_trial
from app.modules.notifications import email as mail
from app.modules.tenants.models import Tenant, TenantMembership, TenantRole

MFA_SECRET_AAD = "user.mfa_secret"


def _check_password(password: str, email: str) -> None:
    problems = validate_password_strength(password, email)
    if problems:
        raise ValidationFailed(problems[0], code="weak_password", details={"problems": problems})


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "business"
    return f"{base}-{secrets.token_hex(3)}"


async def _create_action_token(session: AsyncSession, user: User, purpose: TokenPurpose, ttl: int) -> str:
    # Only the newest token of a purpose stays valid.
    await session.execute(
        update(UserActionToken)
        .where(
            UserActionToken.user_id == user.id,
            UserActionToken.purpose == purpose.value,
            UserActionToken.used_at.is_(None),
        )
        .values(used_at=utcnow())
    )
    plain = generate_token(32)
    session.add(
        UserActionToken(
            user_id=user.id,
            purpose=purpose.value,
            token_hash=hash_token(plain),
            expires_at=utcnow() + timedelta(seconds=ttl),
        )
    )
    return plain


async def _consume_action_token(session: AsyncSession, plain: str, purpose: TokenPurpose) -> User:
    record = (
        await session.execute(
            select(UserActionToken)
            .where(UserActionToken.token_hash == hash_token(plain), UserActionToken.purpose == purpose.value)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None or record.used_at is not None or record.expires_at <= utcnow():
        raise ValidationFailed("This link is invalid or has expired.", code="invalid_or_expired_token")
    record.used_at = utcnow()
    user = await session.get(User, record.user_id)
    if user is None or not user.is_active:
        raise ValidationFailed("This link is invalid or has expired.", code="invalid_or_expired_token")
    return user


async def send_verification(session: AsyncSession, user: User) -> None:
    settings = get_settings()
    token = await _create_action_token(
        session, user, TokenPurpose.EMAIL_VERIFICATION, settings.email_token_ttl_seconds
    )
    mail.send_email(
        mail.verification_email(user.email, f"{settings.frontend_base_url}/verify-email?token={token}")
    )


# --------------------------------------------------------------------- invitations


class InvitationInfo:
    def __init__(self, row: dict[str, object]):
        self.id = uuid.UUID(str(row["id"]))
        self.tenant_id = uuid.UUID(str(row["tenant_id"]))
        self.tenant_name = str(row["tenant_name"])
        self.email = str(row["email"])
        self.role = TenantRole(str(row["role"]))
        self.expires_at = row["expires_at"]


async def find_valid_invitation(session: AsyncSession, plain: str) -> InvitationInfo:
    row = (
        (
            await session.execute(
                text("SELECT * FROM app_invitation_by_token_hash(:h)"), {"h": hash_token(plain)}
            )
        )
        .mappings()
        .first()
    )
    if (
        row is None
        or row["accepted_at"] is not None
        or row["revoked_at"] is not None
        or row["expires_at"] <= utcnow()
    ):
        raise NotFound("This invitation is invalid or has expired.", code="invitation_invalid")
    return InvitationInfo(dict(row))


async def _join_tenant(
    session: AsyncSession, user: User, invitation: InvitationInfo, meta: RequestMeta | None
) -> None:
    from app.modules.tenants.models import TenantInvitation

    await set_tenant_context(session, invitation.tenant_id, user.id)
    existing = (
        await session.execute(
            select(TenantMembership).where(
                TenantMembership.tenant_id == invitation.tenant_id, TenantMembership.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict("You are already a member of this business.", code="already_member")
    session.add(TenantMembership(tenant_id=invitation.tenant_id, user_id=user.id, role=invitation.role.value))
    inv = await session.get(TenantInvitation, invitation.id, with_for_update=True)
    if inv is None or inv.accepted_at is not None:
        raise NotFound("This invitation is invalid or has expired.", code="invitation_invalid")
    inv.accepted_at = utcnow()
    await audit.record(
        session,
        "member.joined",
        tenant_id=invitation.tenant_id,
        actor_user_id=user.id,
        meta=meta,
        target_type="user",
        target_id=user.id,
        details={"role": invitation.role.value},
    )


# --------------------------------------------------------------------- registration


async def register(session: AsyncSession, data: RegisterRequest, meta: RequestMeta) -> IssuedSession:
    _check_password(data.password, data.email)
    invitation: InvitationInfo | None = None
    if data.invitation_token:
        invitation = await find_valid_invitation(session, data.invitation_token)
        if invitation.email.lower() != data.email.lower():
            raise ValidationFailed(
                "Use the email address the invitation was sent to.", code="invitation_email_mismatch"
            )
    elif not data.business_name:
        raise ValidationFailed("Business name is required.", code="business_name_required")

    exists = await session.scalar(select(func.count()).select_from(User).where(User.email == data.email))
    if exists:
        raise Conflict("An account with this email already exists.", code="email_taken")

    user = User(
        id=uuid7(),
        email=data.email,
        password_hash=hash_password(data.password),
        full_name=data.full_name,
        # An accepted invitation proves the address, because the token was emailed to it.
        email_verified_at=utcnow() if invitation else None,
    )
    session.add(user)
    await session.flush()

    if invitation:
        await _join_tenant(session, user, invitation, meta)
        tenant_id = invitation.tenant_id
    else:
        tenant_id = uuid7()
        await set_tenant_context(session, tenant_id, user.id)
        assert data.business_name is not None
        session.add(
            Tenant(
                id=tenant_id,
                name=data.business_name,
                slug=_slugify(data.business_name),
                country=data.country,
                timezone=data.timezone,
            )
        )
        await session.flush()
        session.add(TenantMembership(tenant_id=tenant_id, user_id=user.id, role=TenantRole.OWNER.value))
        await start_trial(session, tenant_id)
        await audit.record(
            session,
            "tenant.created",
            tenant_id=tenant_id,
            actor_user_id=user.id,
            meta=meta,
            target_type="tenant",
            target_id=tenant_id,
        )
        await send_verification(session, user)

    await audit.record(session, "user.registered", tenant_id=tenant_id, actor_user_id=user.id, meta=meta)
    await session.flush()
    return await issue_session(session, user, tenant_id, mfa_verified=False, meta=meta)


# --------------------------------------------------------------------- login


async def _lockout_check(email: str) -> None:
    settings = get_settings()
    failures = await get_redis().get(f"login_fail:{email.lower()}")
    if failures and int(failures) >= settings.login_max_failures:
        ttl = await get_redis().ttl(f"login_fail:{email.lower()}")
        from app.core.errors import TooManyRequests

        raise TooManyRequests(
            retry_after=max(ttl, 1), message="Too many failed sign-in attempts. Try again later."
        )


async def _register_failure(email: str, meta: RequestMeta, user_id: uuid.UUID | None) -> None:
    settings = get_settings()
    key = f"login_fail:{email.lower()}"
    count = await get_redis().incr(key)
    if count == 1:
        await get_redis().expire(key, settings.login_lockout_seconds)
    async with app_transaction() as side:
        await audit.record(
            side,
            "auth.login_failed",
            tenant_id=None,
            actor_user_id=user_id,
            meta=meta,
            details={"attempt": count},
        )


async def login(session: AsyncSession, email: str, password: str, meta: RequestMeta) -> IssuedSession | str:
    """Returns an issued session, or an MFA challenge token if MFA is enabled."""
    await _lockout_check(email)
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not verify_password(password, user.password_hash if user else None) or user is None:
        await _register_failure(email, meta, user.id if user else None)
        raise Unauthorized("Email or password is incorrect.", code="invalid_credentials")
    if not user.is_active:
        raise Unauthorized("This account is disabled.", code="account_inactive")
    await get_redis().delete(f"login_fail:{email.lower()}")
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    if user.mfa_enabled:
        return encode_token(
            {"sub": str(user.id)},
            ttl_seconds=get_settings().mfa_challenge_ttl_seconds,
            token_type="mfa_challenge",
        )
    return await _complete_login(session, user, mfa_verified=False, meta=meta)


async def _complete_login(
    session: AsyncSession, user: User, *, mfa_verified: bool, meta: RequestMeta
) -> IssuedSession:
    memberships = await list_memberships(session, user.id)
    tenant_id = pick_tenant(memberships, user.last_tenant_id)
    user.last_login_at = utcnow()
    await set_tenant_context(session, tenant_id, user.id)
    await audit.record(
        session,
        "auth.login",
        tenant_id=tenant_id,
        actor_user_id=user.id,
        meta=meta,
        details={"mfa": mfa_verified},
    )
    return await issue_session(
        session, user, tenant_id, mfa_verified=mfa_verified, meta=meta, memberships=memberships
    )


def _use_mfa_code(user: User, code: str) -> bool:
    """Accepts a TOTP code or a one-time recovery code (which is then consumed)."""
    if user.mfa_secret_enc and totp.verify_code(crypto.decrypt(user.mfa_secret_enc, MFA_SECRET_AAD), code):
        return True
    hashed = hash_token(code.strip().lower())
    if hashed in user.mfa_recovery_code_hashes:
        user.mfa_recovery_code_hashes = [h for h in user.mfa_recovery_code_hashes if h != hashed]
        return True
    return False


async def verify_mfa_login(
    session: AsyncSession, mfa_token: str, code: str, meta: RequestMeta
) -> IssuedSession:
    claims = decode_token(mfa_token, expected_type="mfa_challenge")
    # Challenge tokens are single-use.
    if not await get_redis().set(
        f"mfa_jti:{claims['jti']}", "1", nx=True, ex=get_settings().mfa_challenge_ttl_seconds
    ):
        raise Unauthorized("This sign-in attempt was already used.", code="mfa_token_used")
    user = await session.get(User, uuid.UUID(claims["sub"]), with_for_update=True)
    if user is None or not user.is_active or not user.mfa_enabled:
        raise Unauthorized(code="invalid_token")
    await _lockout_check(user.email)
    if not _use_mfa_code(user, code):
        await get_redis().delete(f"mfa_jti:{claims['jti']}")
        await _register_failure(user.email, meta, user.id)
        raise Unauthorized("The code is incorrect.", code="invalid_mfa_code")
    await get_redis().delete(f"login_fail:{user.email.lower()}")
    return await _complete_login(session, user, mfa_verified=True, meta=meta)


# --------------------------------------------------------------------- email + passwords


async def verify_email(session: AsyncSession, token: str, meta: RequestMeta) -> None:
    user = await _consume_action_token(session, token, TokenPurpose.EMAIL_VERIFICATION)
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
        await audit.record(session, "user.email_verified", tenant_id=None, actor_user_id=user.id, meta=meta)


async def forgot_password(session: AsyncSession, email: str) -> None:
    """Always succeeds from the caller's view, so accounts cannot be enumerated."""
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None or not user.is_active:
        return
    settings = get_settings()
    token = await _create_action_token(
        session, user, TokenPurpose.PASSWORD_RESET, settings.password_reset_ttl_seconds
    )
    mail.send_email(
        mail.password_reset_email(user.email, f"{settings.frontend_base_url}/reset-password?token={token}")
    )


async def _set_password(session: AsyncSession, user: User, password: str) -> None:
    from app.modules.auth.sessions import revoke_all_for_user

    _check_password(password, user.email)
    user.password_hash = hash_password(password)
    user.password_changed_at = utcnow()
    await revoke_all_for_user(session, user.id, "password_changed")


async def reset_password(session: AsyncSession, token: str, password: str, meta: RequestMeta) -> None:
    user = await _consume_action_token(session, token, TokenPurpose.PASSWORD_RESET)
    await _set_password(session, user, password)
    # The reset link proves control of the inbox.
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
    await audit.record(session, "auth.password_reset", tenant_id=None, actor_user_id=user.id, meta=meta)


async def change_password(
    session: AsyncSession, user: User, current: str, new: str, tenant_id: uuid.UUID | None, meta: RequestMeta
) -> IssuedSession:
    if not verify_password(current, user.password_hash):
        raise Unauthorized("Current password is incorrect.", code="invalid_credentials")
    await _set_password(session, user, new)
    await audit.record(
        session, "auth.password_changed", tenant_id=tenant_id, actor_user_id=user.id, meta=meta
    )
    # Issue a fresh session; every other session was revoked.
    return await issue_session(session, user, tenant_id, mfa_verified=user.mfa_enabled, meta=meta)


# --------------------------------------------------------------------- MFA enrolment


async def mfa_setup(user: User) -> tuple[str, str]:
    if user.mfa_enabled:
        raise Conflict("Two-factor authentication is already enabled.", code="mfa_already_enabled")
    secret = totp.new_secret()
    user.mfa_secret_enc = crypto.encrypt(secret, MFA_SECRET_AAD)
    return secret, totp.provisioning_uri(secret, user.email)


async def mfa_enable(
    session: AsyncSession, user: User, code: str, tenant_id: uuid.UUID | None, meta: RequestMeta
) -> list[str]:
    if user.mfa_enabled:
        raise Conflict("Two-factor authentication is already enabled.", code="mfa_already_enabled")
    if not user.mfa_secret_enc:
        raise ValidationFailed("Start two-factor setup first.", code="mfa_setup_missing")
    if not totp.verify_code(crypto.decrypt(user.mfa_secret_enc, MFA_SECRET_AAD), code):
        raise ValidationFailed("The code is incorrect.", code="invalid_mfa_code")
    codes, hashes = totp.generate_recovery_codes()
    user.mfa_enabled_at = utcnow()
    user.mfa_recovery_code_hashes = hashes
    await audit.record(session, "auth.mfa_enabled", tenant_id=tenant_id, actor_user_id=user.id, meta=meta)
    return codes


async def mfa_disable(
    session: AsyncSession,
    user: User,
    password: str,
    code: str,
    tenant_id: uuid.UUID | None,
    meta: RequestMeta,
) -> None:
    if user.is_super_admin:
        raise Forbidden(
            "Super Admin accounts must keep two-factor authentication.", code="mfa_required_for_role"
        )
    if not user.mfa_enabled:
        raise Conflict("Two-factor authentication is not enabled.", code="mfa_not_enabled")
    if not verify_password(password, user.password_hash) or not _use_mfa_code(user, code):
        raise Unauthorized("Password or code is incorrect.", code="invalid_credentials")
    user.mfa_enabled_at = None
    user.mfa_secret_enc = None
    user.mfa_recovery_code_hashes = []
    await audit.record(session, "auth.mfa_disabled", tenant_id=tenant_id, actor_user_id=user.id, meta=meta)


async def accept_invitation(session: AsyncSession, user: User, token: str, meta: RequestMeta) -> uuid.UUID:
    invitation = await find_valid_invitation(session, token)
    if invitation.email.lower() != user.email.lower():
        raise Forbidden(
            "This invitation was sent to a different email address.", code="invitation_email_mismatch"
        )
    if not user.email_verified:
        raise Forbidden("Verify your email address first.", code="email_not_verified")
    await _join_tenant(session, user, invitation, meta)
    return invitation.tenant_id
