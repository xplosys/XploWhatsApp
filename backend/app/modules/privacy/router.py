"""Security and privacy settings for a business, and the personal data tools."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select

from app.api.deps import Meta, Requires, TenantContext
from app.core.errors import Forbidden, NotFound, Unauthorized, ValidationFailed
from app.core.rate_limit import hit
from app.core.security.passwords import verify_password
from app.db.session import run_after_commit
from app.modules.audit import service as audit
from app.modules.auth.sessions import revoke_family
from app.modules.conversations import inbox
from app.modules.privacy import service
from app.modules.privacy.models import DataExport, ExportStatus
from app.modules.rbac.permissions import Permission
from app.modules.storage import s3
from app.workers.dispatch import DATA_EXPORT, dispatch

router = APIRouter(tags=["privacy"])
Owner = Annotated[TenantContext, Requires(Permission.SECURITY_MANAGE)]
PrivacyManager = Annotated[TenantContext, Requires(Permission.PRIVACY_MANAGE)]


class SecuritySettingsOut(BaseModel):
    require_mfa: bool
    message_retention_days: int | None
    members_without_mfa: int


class SecuritySettingsUpdate(BaseModel):
    require_mfa: bool | None = None
    # null keeps messages for as long as the business exists.
    message_retention_days: Annotated[int, Field(ge=30, le=3650)] | None = None


async def _settings_out(ctx: TenantContext) -> SecuritySettingsOut:
    from app.modules.auth.models import User
    from app.modules.tenants.models import TenantMembership

    without = (
        await ctx.session.execute(
            select(User.id)
            .join(TenantMembership, TenantMembership.user_id == User.id)
            .where(
                TenantMembership.tenant_id == ctx.tenant_id,
                TenantMembership.status == "active",
                User.mfa_enabled_at.is_(None),
            )
        )
    ).all()
    return SecuritySettingsOut(
        require_mfa=ctx.tenant.require_mfa,
        message_retention_days=ctx.tenant.message_retention_days,
        members_without_mfa=len(without),
    )


@router.get("/tenants/current/security", response_model=SecuritySettingsOut)
async def get_security(ctx: Owner) -> SecuritySettingsOut:
    return await _settings_out(ctx)


@router.put("/tenants/current/security", response_model=SecuritySettingsOut)
async def update_security(body: SecuritySettingsUpdate, ctx: Owner, meta: Meta) -> SecuritySettingsOut:
    """Require two-factor sign-in for the whole team, and choose how long messages are kept."""
    tenant = ctx.tenant
    changes: dict[str, object] = {}
    fields = body.model_fields_set
    if "require_mfa" in fields and body.require_mfa is not None and body.require_mfa != tenant.require_mfa:
        if body.require_mfa and not ctx.claims.mfa_verified:
            raise Forbidden(
                "Turn on two-factor authentication for your own account and sign in with it first.",
                code="mfa_required",
            )
        tenant.require_mfa = body.require_mfa
        changes["require_mfa"] = body.require_mfa
    if "message_retention_days" in fields and body.message_retention_days != tenant.message_retention_days:
        tenant.message_retention_days = body.message_retention_days
        changes["message_retention_days"] = body.message_retention_days
    if changes:
        await audit.record(
            ctx.session,
            "tenant.security_updated",
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="tenant",
            target_id=ctx.tenant_id,
            details=changes,
        )
    return await _settings_out(ctx)


# ---------------------------------------------------------------------- erasure


class ErasureOut(BaseModel):
    conversations: int
    messages: int
    leads: int


@router.post("/contacts/{contact_id}/erase", response_model=ErasureOut)
async def erase_contact(contact_id: uuid.UUID, ctx: PrivacyManager, meta: Meta) -> ErasureOut:
    """Erase a customer on their request: the contact, conversations, messages, notes,
    summaries and leads. Cannot be undone."""
    contact = await inbox.get_contact(ctx.session, ctx.tenant_id, contact_id)
    counts = await service.erase_contact(ctx.session, ctx.tenant_id, contact, ctx.user.id, meta)
    return ErasureOut(**counts)


# ---------------------------------------------------------------------- export


class ExportOut(BaseModel):
    id: uuid.UUID
    status: Literal["queued", "running", "ready", "failed", "expired"]
    size_bytes: int | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None


class DownloadOut(BaseModel):
    url: str


def _export_out(e: DataExport) -> ExportOut:
    return ExportOut(
        id=e.id,
        status=e.status,
        size_bytes=e.size_bytes,
        error=e.error,
        created_at=e.created_at,
        completed_at=e.completed_at,
        expires_at=e.expires_at,
    )


@router.get("/privacy/exports", response_model=list[ExportOut])
async def list_exports(ctx: Owner) -> list[ExportOut]:
    rows = (
        await ctx.session.execute(
            select(DataExport)
            .where(DataExport.tenant_id == ctx.tenant_id)
            .order_by(DataExport.created_at.desc())
            .limit(20)
        )
    ).scalars()
    return [_export_out(e) for e in rows]


@router.post("/privacy/exports", response_model=ExportOut, status_code=status.HTTP_202_ACCEPTED)
async def request_export(ctx: Owner, meta: Meta) -> ExportOut:
    """Export everything the business holds as JSON Lines in a zip file."""
    if not s3.storage_configured():
        raise s3.StorageUnavailable()
    export = await service.request_export(ctx.session, ctx.tenant_id, ctx.user.id)
    await audit.record(
        ctx.session,
        "privacy.export_requested",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="data_export",
        target_id=export.id,
    )

    async def _start(export_id: uuid.UUID = export.id, tenant_id: uuid.UUID = ctx.tenant_id) -> None:
        await dispatch(DATA_EXPORT, export_id, tenant_id)

    run_after_commit(ctx.session, _start)
    await ctx.session.refresh(export)
    return _export_out(export)


@router.post("/privacy/exports/{export_id}/download", response_model=DownloadOut)
async def download_export(export_id: uuid.UUID, ctx: Owner, meta: Meta) -> DownloadOut:
    """A link valid for a few minutes. Every download is audited."""
    export = await ctx.session.get(DataExport, export_id)
    if export is None or export.tenant_id != ctx.tenant_id:
        raise NotFound("Export not found.", code="export_not_found")
    if export.status != ExportStatus.READY or not export.object_key:
        raise ValidationFailed("This export isn't available to download.", code="export_not_ready")
    await hit(f"export_download:{ctx.user.id}", 20, 3600)
    await audit.record(
        ctx.session,
        "privacy.export_downloaded",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="data_export",
        target_id=export.id,
    )
    stamp = (export.completed_at or export.created_at).strftime("%Y-%m-%d")
    return DownloadOut(url=await s3.download_url(export.object_key, f"xplowhatsapp-export-{stamp}.zip"))


# ---------------------------------------------------------------------- business deletion


class DeleteBusinessRequest(BaseModel):
    password: Annotated[str, Field(min_length=1, max_length=128)]
    confirm_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class DeleteBusinessOut(BaseModel):
    purge_after: datetime


@router.post("/tenants/current/delete", response_model=DeleteBusinessOut)
async def delete_business(
    body: DeleteBusinessRequest, request: Request, response: Response, ctx: Owner, meta: Meta
) -> DeleteBusinessOut:
    """Delete the business. Everyone loses access now; data is purged after the grace
    period unless support restores it. Tax invoices and the audit log are kept."""
    if request.headers.get("x-requested-with") != "XploWhatsApp":
        raise Forbidden("Missing request header.", code="csrf_check_failed")
    await hit(f"delete_business:{ctx.user.id}", 5, 3600)
    if not verify_password(body.password, ctx.user.password_hash):
        raise Unauthorized("Password is incorrect.", code="invalid_credentials")
    if body.confirm_name.casefold() != ctx.tenant.name.strip().casefold():
        raise ValidationFailed("Type the business name exactly as shown.", code="confirm_name_mismatch")
    purge_after = await service.schedule_business_deletion(ctx.session, ctx.tenant, ctx.user.id, meta)
    # This browser's session was for the deleted business; the next refresh moves it to
    # another business the person belongs to, or to none.
    if ctx.claims.session_id:
        await revoke_family(ctx.session, ctx.claims.session_id, "tenant_deleted")
    await _email_owners_deleted(ctx, purge_after)
    return DeleteBusinessOut(purge_after=purge_after)


async def _email_owners_deleted(ctx: TenantContext, purge_after: datetime) -> None:
    from app.modules.billing.notices import recipients
    from app.modules.notifications.email import OutgoingEmail, send_email

    name = ctx.tenant.name
    for to in await recipients(ctx.session, ctx.tenant_id):
        email = OutgoingEmail(
            to=to,
            subject=f"{name} was deleted from XploWhatsApp",
            text=(
                f"{ctx.user.full_name} deleted {name} on XploWhatsApp. Nobody can sign in to it any more, "
                "and the WhatsApp assistant has stopped replying.\n\n"
                f"Its data will be permanently removed on {purge_after:%d %B %Y}. Until then, "
                "XploSys support can restore it if this was a mistake. Tax invoices are kept as the "
                "law requires."
            ),
        )

        async def _send(email: OutgoingEmail = email) -> None:
            send_email(email)

        run_after_commit(ctx.session, _send)
