from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import CurrentUser, DbSession, Meta
from app.core.config import get_settings
from app.core.errors import Forbidden, Unauthorized
from app.core.rate_limit import rate_limit
from app.modules.audit import service as audit
from app.modules.auth import service
from app.modules.auth.schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    InvitationPreviewOut,
    LoginRequest,
    MeOut,
    MessageOut,
    MfaChallengeOut,
    MfaDisableRequest,
    MfaEnableRequest,
    MfaSetupOut,
    MfaVerifyRequest,
    RecoveryCodesOut,
    RegisterRequest,
    ResetPasswordRequest,
    SessionOut,
    SwitchTenantRequest,
    TokenRequest,
    UserOut,
)
from app.modules.auth.sessions import (
    IssuedSession,
    end_session,
    issue_session,
    list_memberships,
    revoke_family,
    rotate_session,
)
from app.modules.rbac.permissions import ROLE_PERMISSIONS
from app.modules.tenants.models import TenantRole

router = APIRouter(prefix="/auth", tags=["auth"])

auth_rate_limit = Depends(rate_limit("auth", lambda: get_settings().rate_limit_auth_per_minute))

CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "XploWhatsApp"


def _set_refresh_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        key=s.refresh_cookie_name,
        value=token,
        max_age=s.refresh_token_ttl_seconds,
        path=s.refresh_cookie_path,
        domain=s.cookie_domain,
        secure=s.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(
        key=s.refresh_cookie_name,
        path=s.refresh_cookie_path,
        domain=s.cookie_domain,
        secure=s.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _deliver(response: Response, issued: IssuedSession) -> SessionOut:
    _set_refresh_cookie(response, issued.refresh_token)
    return issued.body


def _require_csrf_header(request: Request) -> None:
    """Cookie-authenticated endpoints need a custom header, which cross-site
    forms cannot send without a CORS preflight (on top of SameSite=Strict)."""
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise Forbidden("Missing request header.", code="csrf_check_failed")


def _refresh_cookie(request: Request) -> str:
    token = request.cookies.get(get_settings().refresh_cookie_name)
    if not token:
        raise Unauthorized("No active session.", code="no_session")
    return token


@router.post(
    "/register",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[auth_rate_limit],
)
async def register(body: RegisterRequest, response: Response, session: DbSession, meta: Meta) -> SessionOut:
    """Create an account. Without an invitation this also creates the business (tenant),
    makes the user its owner and starts the free trial."""
    return _deliver(response, await service.register(session, body, meta))


@router.post("/login", response_model=SessionOut | MfaChallengeOut, dependencies=[auth_rate_limit])
async def login(
    body: LoginRequest, response: Response, session: DbSession, meta: Meta
) -> SessionOut | MfaChallengeOut:
    result = await service.login(session, body.email, body.password, meta)
    if isinstance(result, str):
        return MfaChallengeOut(mfa_token=result)
    return _deliver(response, result)


@router.post("/mfa/verify", response_model=SessionOut, dependencies=[auth_rate_limit])
async def mfa_verify(
    body: MfaVerifyRequest, response: Response, session: DbSession, meta: Meta
) -> SessionOut:
    return _deliver(response, await service.verify_mfa_login(session, body.mfa_token, body.code, meta))


@router.post("/refresh", response_model=SessionOut, dependencies=[auth_rate_limit])
async def refresh(request: Request, response: Response, session: DbSession, meta: Meta) -> SessionOut:
    """Rotate the refresh cookie and return a new access token. Reusing an old
    refresh token signs out every device in that session family."""
    _require_csrf_header(request)
    return _deliver(response, await rotate_session(session, _refresh_cookie(request), meta))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, session: DbSession, meta: Meta) -> Response:
    _require_csrf_header(request)
    token = request.cookies.get(get_settings().refresh_cookie_name)
    if token:
        user_id = await end_session(session, token)
        if user_id:
            await audit.record(session, "auth.logout", tenant_id=None, actor_user_id=user_id, meta=meta)
    response.status_code = status.HTTP_204_NO_CONTENT
    _clear_refresh_cookie(response)
    return response


@router.post("/switch-tenant", response_model=SessionOut)
async def switch_tenant(
    body: SwitchTenantRequest, response: Response, ctx: CurrentUser, meta: Meta
) -> SessionOut:
    memberships = await list_memberships(ctx.session, ctx.user.id)
    if body.tenant_id not in {m.tenant_id for m in memberships if m.status == "active"}:
        raise Forbidden("You are not a member of this business.", code="not_a_member")
    if ctx.claims.session_id:
        await revoke_family(ctx.session, ctx.claims.session_id, "switched_tenant")
    return _deliver(
        response,
        await issue_session(
            ctx.session,
            ctx.user,
            body.tenant_id,
            mfa_verified=ctx.claims.mfa_verified,
            meta=meta,
            memberships=memberships,
        ),
    )


@router.get("/me", response_model=MeOut)
async def me(ctx: CurrentUser) -> MeOut:
    memberships = await list_memberships(ctx.session, ctx.user.id)
    role: TenantRole | None = None
    for m in memberships:
        if m.tenant_id == ctx.claims.tenant_id and m.status == "active":
            role = m.role
    return MeOut(
        user=UserOut.model_validate(ctx.user),
        active_tenant_id=ctx.claims.tenant_id if role else None,
        role=role,
        permissions=sorted(p.value for p in ROLE_PERMISSIONS[role]) if role else [],
        memberships=memberships,
        mfa_verified=ctx.claims.mfa_verified,
    )


@router.post("/verify-email", response_model=MessageOut, dependencies=[auth_rate_limit])
async def verify_email(body: TokenRequest, session: DbSession, meta: Meta) -> MessageOut:
    await service.verify_email(session, body.token, meta)
    return MessageOut(message="Email verified.")


@router.post("/resend-verification", response_model=MessageOut, dependencies=[auth_rate_limit])
async def resend_verification(ctx: CurrentUser) -> MessageOut:
    if ctx.user.email_verified:
        return MessageOut(message="Email is already verified.")
    await service.send_verification(ctx.session, ctx.user)
    return MessageOut(message="Verification email sent.")


@router.post(
    "/password/forgot",
    response_model=MessageOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[auth_rate_limit],
)
async def forgot_password(body: ForgotPasswordRequest, session: DbSession) -> MessageOut:
    await service.forgot_password(session, body.email)
    return MessageOut(message="If an account exists for this email, a reset link has been sent.")


@router.post("/password/reset", response_model=MessageOut, dependencies=[auth_rate_limit])
async def reset_password(body: ResetPasswordRequest, session: DbSession, meta: Meta) -> MessageOut:
    await service.reset_password(session, body.token, body.password, meta)
    return MessageOut(message="Password updated. Please sign in.")


@router.post("/password/change", response_model=SessionOut, dependencies=[auth_rate_limit])
async def change_password(
    body: ChangePasswordRequest, response: Response, ctx: CurrentUser, meta: Meta
) -> SessionOut:
    issued = await service.change_password(
        ctx.session, ctx.user, body.current_password, body.new_password, ctx.claims.tenant_id, meta
    )
    return _deliver(response, issued)


@router.post("/mfa/setup", response_model=MfaSetupOut)
async def mfa_setup(ctx: CurrentUser) -> MfaSetupOut:
    secret, uri = await service.mfa_setup(ctx.user)
    return MfaSetupOut(secret=secret, otpauth_uri=uri)


@router.post("/mfa/enable", response_model=RecoveryCodesOut, dependencies=[auth_rate_limit])
async def mfa_enable(body: MfaEnableRequest, ctx: CurrentUser, meta: Meta) -> RecoveryCodesOut:
    codes = await service.mfa_enable(ctx.session, ctx.user, body.code, ctx.claims.tenant_id, meta)
    return RecoveryCodesOut(recovery_codes=codes)


@router.post("/mfa/disable", response_model=MessageOut, dependencies=[auth_rate_limit])
async def mfa_disable(body: MfaDisableRequest, ctx: CurrentUser, meta: Meta) -> MessageOut:
    await service.mfa_disable(ctx.session, ctx.user, body.password, body.code, ctx.claims.tenant_id, meta)
    return MessageOut(message="Two-factor authentication disabled.")


@router.get("/invitations/{token}", response_model=InvitationPreviewOut, dependencies=[auth_rate_limit])
async def invitation_preview(token: str, session: DbSession) -> InvitationPreviewOut:
    inv = await service.find_valid_invitation(session, token)
    return InvitationPreviewOut(
        tenant_name=inv.tenant_name,
        email=inv.email,
        role=inv.role,
        expires_at=inv.expires_at,
    )


@router.post("/invitations/accept", response_model=SessionOut, dependencies=[auth_rate_limit])
async def accept_invitation(
    body: TokenRequest, response: Response, ctx: CurrentUser, meta: Meta
) -> SessionOut:
    """Join the inviting business and switch the session to it."""
    tenant_id = await service.accept_invitation(ctx.session, ctx.user, body.token, meta)
    if ctx.claims.session_id:
        await revoke_family(ctx.session, ctx.claims.session_id, "switched_tenant")
    await ctx.session.flush()
    return _deliver(
        response,
        await issue_session(
            ctx.session, ctx.user, tenant_id, mfa_verified=ctx.claims.mfa_verified, meta=meta
        ),
    )
