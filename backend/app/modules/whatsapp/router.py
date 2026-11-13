"""WhatsApp connection management: Embedded Signup, accounts, numbers, templates."""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import Meta, Requires, TenantContext
from app.core.config import get_settings
from app.core.errors import AppError, Forbidden, NotFound, ValidationFailed
from app.core.redis import get_redis
from app.modules.audit.service import record
from app.modules.billing.service import effective_plan
from app.modules.rbac.permissions import Permission
from app.modules.whatsapp import service
from app.modules.whatsapp.graph import GraphClient, GraphError
from app.modules.whatsapp.models import (
    AccountStatus,
    CredentialKind,
    MessageTemplate,
    NumberStatus,
    WhatsAppAccount,
    WhatsAppNumber,
)
from app.modules.whatsapp.schemas import (
    AccountOut,
    DisconnectOut,
    NumberOut,
    OnboardingComplete,
    OnboardingSessionOut,
    PinOut,
    SignupConfigOut,
    TemplateOut,
    TemplateSyncOut,
)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])

Reader = Annotated[TenantContext, Requires(Permission.WHATSAPP_READ)]
Manager = Annotated[TenantContext, Requires(Permission.WHATSAPP_MANAGE)]

ONBOARDING_TTL_SECONDS = 15 * 60


class SignupNotConfigured(AppError):
    status_code = 503
    code = "embedded_signup_not_configured"
    message = "WhatsApp sign-up is not configured on this server yet."


def _missing_signup_settings() -> list[str]:
    s = get_settings()
    missing = []
    if not s.meta_app_id:
        missing.append("META_APP_ID")
    if not s.meta_app_secret:
        missing.append("META_APP_SECRET")
    if not s.meta_embedded_signup_config_id:
        missing.append("META_EMBEDDED_SIGNUP_CONFIG_ID")
    if not s.meta_webhook_verify_token:
        missing.append("META_WEBHOOK_VERIFY_TOKEN")
    return missing


async def _account_out(ctx: TenantContext, account: WhatsAppAccount) -> AccountOut:
    out = AccountOut.model_validate(account)
    out.numbers = [NumberOut.model_validate(n) for n in await service.numbers_for(ctx.session, account.id)]
    return out


@router.get("/signup-config", response_model=SignupConfigOut)
async def signup_config(_: Reader) -> SignupConfigOut:
    s = get_settings()
    missing = _missing_signup_settings()
    return SignupConfigOut(
        enabled=not missing,
        app_id=s.meta_app_id,
        config_id=s.meta_embedded_signup_config_id,
        graph_api_version=s.meta_graph_api_version,
        embedded_signup_version=s.meta_embedded_signup_version,
        missing=missing,
    )


async def _connected_number_count(ctx: TenantContext) -> int:
    return int(
        await ctx.session.scalar(
            select(func.count())
            .select_from(WhatsAppNumber)
            .where(WhatsAppNumber.tenant_id == ctx.tenant_id, WhatsAppNumber.status == NumberStatus.CONNECTED)
        )
        or 0
    )


@router.post("/onboarding/sessions", response_model=OnboardingSessionOut, status_code=201)
async def start_onboarding(ctx: Manager) -> OnboardingSessionOut:
    """Opens a short-lived sign-up session. The browser must return its ID with the
    Meta code, which ties the code to this user and workspace."""
    if _missing_signup_settings():
        raise SignupNotConfigured()
    session_id = secrets.token_urlsafe(32)
    await get_redis().set(
        f"wa_onboarding:{session_id}",
        json.dumps({"tenant_id": str(ctx.tenant_id), "user_id": str(ctx.user.id)}),
        ex=ONBOARDING_TTL_SECONDS,
    )
    return OnboardingSessionOut(session_id=session_id, expires_in=ONBOARDING_TTL_SECONDS)


@router.post("/onboarding/complete", response_model=AccountOut)
async def complete_onboarding(body: OnboardingComplete, ctx: Manager, meta: Meta) -> AccountOut:
    if _missing_signup_settings():
        raise SignupNotConfigured()
    raw = await get_redis().getdel(f"wa_onboarding:{body.session_id}")
    if raw is None:
        raise ValidationFailed(
            "The sign-up session expired. Start the connection again.", code="onboarding_expired"
        )
    owner = json.loads(raw)
    if owner.get("tenant_id") != str(ctx.tenant_id) or owner.get("user_id") != str(ctx.user.id):
        raise Forbidden("This sign-up session belongs to someone else.", code="onboarding_mismatch")

    existing = await service.account_by_waba_id(ctx.session, ctx.tenant_id, body.waba_id)
    if body.phone_number_id:
        target = await service.number_by_phone_number_id(ctx.session, ctx.tenant_id, body.phone_number_id)
        if target is None or target.status != NumberStatus.CONNECTED:
            plan = await effective_plan(ctx.session, ctx.tenant_id)
            limit = plan.limit("max_whatsapp_numbers", 0)
            if await _connected_number_count(ctx) >= limit:
                raise Forbidden(
                    f"Your plan allows {limit} connected WhatsApp number(s). Upgrade to connect more.",
                    code="plan_limit_reached",
                    details={"limit": limit},
                )

    async with GraphClient() as graph:
        account = await service.complete_onboarding(
            ctx.session,
            ctx.tenant_id,
            ctx.user.id,
            service.OnboardingInput(
                code=body.code,
                waba_id=body.waba_id,
                phone_number_id=body.phone_number_id,
                business_id=body.business_id,
                event=body.event,
            ),
            graph,
        )
    await record(
        ctx.session,
        "whatsapp.reconnected" if existing else "whatsapp.connected",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="whatsapp_account",
        target_id=account.id,
        details={"waba_id": body.waba_id, "phone_number_id": body.phone_number_id, "event": body.event},
    )
    return await _account_out(ctx, account)


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(ctx: Reader) -> list[AccountOut]:
    accounts = (
        await ctx.session.execute(
            select(WhatsAppAccount)
            .where(WhatsAppAccount.tenant_id == ctx.tenant_id)
            .order_by(WhatsAppAccount.status != AccountStatus.CONNECTED, WhatsAppAccount.created_at)
        )
    ).scalars()
    return [await _account_out(ctx, a) for a in accounts]


@router.get("/accounts/{account_id}", response_model=AccountOut)
async def get_account(account_id: uuid.UUID, ctx: Reader) -> AccountOut:
    return await _account_out(ctx, await service.get_account(ctx.session, ctx.tenant_id, account_id))


@router.post("/accounts/{account_id}/health-check", response_model=AccountOut)
async def health_check(account_id: uuid.UUID, ctx: Manager) -> AccountOut:
    account = await service.get_account(ctx.session, ctx.tenant_id, account_id)
    async with GraphClient() as graph:
        await service.check_health(ctx.session, account, graph)
    return await _account_out(ctx, account)


@router.delete("/accounts/{account_id}", response_model=DisconnectOut)
async def disconnect(account_id: uuid.UUID, ctx: Manager, meta: Meta) -> DisconnectOut:
    account = await service.get_account(ctx.session, ctx.tenant_id, account_id)
    if account.status == AccountStatus.DISCONNECTED:
        return DisconnectOut(account=await _account_out(ctx, account), meta_notified=False)
    notified = await service.disconnect(ctx.session, account)
    await record(
        ctx.session,
        "whatsapp.disconnected",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="whatsapp_account",
        target_id=account.id,
        details={"waba_id": account.waba_id, "meta_notified": notified},
    )
    return DisconnectOut(account=await _account_out(ctx, account), meta_notified=notified)


@router.post("/numbers/{number_id}/register", response_model=AccountOut)
async def register_number(number_id: uuid.UUID, ctx: Manager, meta: Meta) -> AccountOut:
    """Retry Cloud API registration for a number that Meta did not register at sign-up."""
    number = await service.get_number(ctx.session, ctx.tenant_id, number_id)
    account = await service.get_account(ctx.session, ctx.tenant_id, number.account_id)
    if account.status != AccountStatus.CONNECTED:
        raise ValidationFailed("Reconnect the account first.", code="account_not_connected")
    try:
        await service.retry_registration(ctx.session, account, number)
    except GraphError as exc:
        raise service.meta_error(exc, "Meta did not register the number.") from exc
    await record(
        ctx.session,
        "whatsapp.number_registered",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="whatsapp_number",
        target_id=number.id,
    )
    return await _account_out(ctx, account)


@router.post("/numbers/{number_id}/pin", response_model=PinOut)
async def reveal_pin(number_id: uuid.UUID, ctx: Manager, meta: Meta) -> PinOut:
    """The two-step verification PIN we set when registering the number. Shown on
    request only, and every view is audited."""
    number = await service.get_number(ctx.session, ctx.tenant_id, number_id)
    account = await service.get_account(ctx.session, ctx.tenant_id, number.account_id)
    pin = await service.read_credential(ctx.session, account, CredentialKind.REGISTRATION_PIN, number.id)
    if pin is None:
        raise NotFound("No PIN is stored for this number.", code="pin_not_found")
    await record(
        ctx.session,
        "whatsapp.pin_viewed",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="whatsapp_number",
        target_id=number.id,
    )
    return PinOut(pin=pin)


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(
    ctx: Reader,
    account_id: uuid.UUID | None = None,
    approved_only: Annotated[bool, Query()] = False,
) -> list[TemplateOut]:
    stmt = select(MessageTemplate).where(
        MessageTemplate.tenant_id == ctx.tenant_id, MessageTemplate.status != "DELETED"
    )
    if account_id:
        stmt = stmt.where(MessageTemplate.account_id == account_id)
    if approved_only:
        stmt = stmt.where(MessageTemplate.status == "APPROVED")
    rows = (
        await ctx.session.execute(stmt.order_by(MessageTemplate.name, MessageTemplate.language))
    ).scalars()
    return [TemplateOut.model_validate(t) for t in rows]


@router.post("/accounts/{account_id}/templates/sync", response_model=TemplateSyncOut)
async def sync_templates(account_id: uuid.UUID, ctx: Manager, meta: Meta) -> TemplateSyncOut:
    account = await service.get_account(ctx.session, ctx.tenant_id, account_id)
    if account.status != AccountStatus.CONNECTED:
        raise ValidationFailed(
            "Reconnect the account before syncing templates.", code="account_not_connected"
        )
    async with GraphClient() as graph:
        count = await service.sync_templates(ctx.session, account, graph)
    await record(
        ctx.session,
        "whatsapp.templates_synced",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="whatsapp_account",
        target_id=account.id,
        details={"templates": count},
    )
    return TemplateSyncOut(synced=count)
