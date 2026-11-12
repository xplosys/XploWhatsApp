"""WhatsApp account lifecycle: credentials, onboarding, health, disconnect, templates."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, Conflict, NotFound, ValidationFailed
from app.core.security import crypto
from app.modules.whatsapp.graph import GraphClient, GraphError
from app.modules.whatsapp.models import (
    AccountHealth,
    AccountStatus,
    CredentialKind,
    MessageTemplate,
    NumberStatus,
    WhatsAppAccount,
    WhatsAppCredential,
    WhatsAppNumber,
)

log = structlog.get_logger(__name__)

REQUIRED_SCOPES = ("whatsapp_business_management", "whatsapp_business_messaging")


class MetaUnavailable(AppError):
    status_code = 502
    code = "meta_error"
    message = "Meta returned an error. Please try again."


def meta_error(exc: GraphError, message: str) -> MetaUnavailable:
    return MetaUnavailable(message, details=exc.public())


# ---- lookups -----------------------------------------------------------------


async def account_by_waba_id(
    session: AsyncSession, tenant_id: uuid.UUID, waba_id: str
) -> WhatsAppAccount | None:
    return (
        await session.execute(
            select(WhatsAppAccount).where(
                WhatsAppAccount.tenant_id == tenant_id, WhatsAppAccount.waba_id == waba_id
            )
        )
    ).scalar_one_or_none()


async def number_by_phone_number_id(
    session: AsyncSession, tenant_id: uuid.UUID, phone_number_id: str
) -> WhatsAppNumber | None:
    return (
        await session.execute(
            select(WhatsAppNumber).where(
                WhatsAppNumber.tenant_id == tenant_id, WhatsAppNumber.phone_number_id == phone_number_id
            )
        )
    ).scalar_one_or_none()


async def get_account(session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID) -> WhatsAppAccount:
    account = await session.get(WhatsAppAccount, account_id)
    if account is None or account.tenant_id != tenant_id:
        raise NotFound("WhatsApp account not found.")
    return account


async def get_number(session: AsyncSession, tenant_id: uuid.UUID, number_id: uuid.UUID) -> WhatsAppNumber:
    number = await session.get(WhatsAppNumber, number_id)
    if number is None or number.tenant_id != tenant_id:
        raise NotFound("Phone number not found.")
    return number


async def numbers_for(session: AsyncSession, account_id: uuid.UUID) -> list[WhatsAppNumber]:
    return list(
        (
            await session.execute(
                select(WhatsAppNumber)
                .where(WhatsAppNumber.account_id == account_id)
                .order_by(WhatsAppNumber.created_at)
            )
        ).scalars()
    )


def add_health_issue(
    account: WhatsAppAccount, code: str, message: str, *, level: str = "error", details: Any = None
) -> None:
    issues = [i for i in (account.health_issues or []) if i.get("code") != code]
    issues.append(
        {
            "code": code,
            "level": level,
            "message": message,
            "at": datetime.now(UTC).isoformat(),
            "details": details,
        }
    )
    account.health_issues = issues[-20:]


# ---- credentials -------------------------------------------------------------


def _aad(tenant_id: uuid.UUID, account_id: uuid.UUID, kind: str) -> str:
    return f"whatsapp:{tenant_id}:{account_id}:{kind}"


async def store_credential(
    session: AsyncSession,
    account: WhatsAppAccount,
    kind: CredentialKind,
    secret: str,
    *,
    number_id: uuid.UUID | None = None,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
) -> None:
    now = datetime.now(UTC)
    revoke = update(WhatsAppCredential).where(
        WhatsAppCredential.account_id == account.id,
        WhatsAppCredential.kind == kind,
        WhatsAppCredential.revoked_at.is_(None),
    )
    if number_id is not None:
        revoke = revoke.where(WhatsAppCredential.number_id == number_id)
    await session.execute(revoke.values(revoked_at=now, ciphertext=None))
    ciphertext = crypto.encrypt(secret, _aad(account.tenant_id, account.id, kind))
    session.add(
        WhatsAppCredential(
            tenant_id=account.tenant_id,
            account_id=account.id,
            number_id=number_id,
            kind=kind,
            ciphertext=ciphertext,
            key_version=crypto.key_version_of(ciphertext),
            scopes=scopes or [],
            expires_at=expires_at,
        )
    )
    await session.flush()


async def read_credential(
    session: AsyncSession, account: WhatsAppAccount, kind: CredentialKind, number_id: uuid.UUID | None = None
) -> str | None:
    stmt = (
        select(WhatsAppCredential)
        .where(
            WhatsAppCredential.account_id == account.id,
            WhatsAppCredential.kind == kind,
            WhatsAppCredential.revoked_at.is_(None),
        )
        .order_by(WhatsAppCredential.created_at.desc())
        .limit(1)
    )
    if number_id is not None:
        stmt = stmt.where(WhatsAppCredential.number_id == number_id)
    cred = (await session.execute(stmt)).scalar_one_or_none()
    if cred is None or cred.ciphertext is None:
        return None
    return crypto.decrypt(cred.ciphertext, _aad(account.tenant_id, account.id, kind))


async def business_token(session: AsyncSession, account: WhatsAppAccount) -> str:
    token = await read_credential(session, account, CredentialKind.BUSINESS_TOKEN)
    if token is None:
        raise AppError("Reconnect this WhatsApp account.", code="account_needs_reconnect")
    return token


async def revoke_all_credentials(session: AsyncSession, account: WhatsAppAccount) -> None:
    await session.execute(
        update(WhatsAppCredential)
        .where(WhatsAppCredential.account_id == account.id, WhatsAppCredential.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), ciphertext=None)
    )


# ---- onboarding (Embedded Signup, Tech Provider) -------------------------------


@dataclass
class OnboardingInput:
    code: str
    waba_id: str
    phone_number_id: str | None
    business_id: str | None
    event: str | None


def generate_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _apply_number_fields(number: WhatsAppNumber, data: dict[str, Any]) -> None:
    number.display_phone_number = data.get("display_phone_number") or number.display_phone_number
    number.verified_name = data.get("verified_name") or number.verified_name
    number.quality_rating = data.get("quality_rating") or number.quality_rating
    number.code_verification_status = data.get("code_verification_status") or number.code_verification_status
    number.name_status = data.get("name_status") or number.name_status
    number.platform_type = data.get("platform_type") or number.platform_type
    number.meta_status = data.get("status") or number.meta_status
    tier = data.get("messaging_limit_tier") or (data.get("throughput") or {}).get("level")
    if tier:
        number.messaging_limit_tier = str(tier)[:32]


async def _waba_owner(session: AsyncSession, waba_id: str) -> uuid.UUID | None:
    row = (
        await session.execute(text("SELECT tenant_id FROM app_resolve_whatsapp_account(:w)"), {"w": waba_id})
    ).first()
    return row.tenant_id if row else None


async def _number_owner(session: AsyncSession, phone_number_id: str) -> uuid.UUID | None:
    row = (
        await session.execute(
            text("SELECT tenant_id FROM app_resolve_whatsapp_number(:p)"), {"p": phone_number_id}
        )
    ).first()
    return row.tenant_id if row else None


async def complete_onboarding(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    data: OnboardingInput,
    graph: GraphClient,
) -> WhatsAppAccount:
    """Server side of Embedded Signup. The browser's IDs are hints: every one is
    re-verified against Graph API with the token Meta issued for this business."""
    owner = await _waba_owner(session, data.waba_id)
    if owner is not None and owner != tenant_id:
        raise Conflict(
            "This WhatsApp Business Account is already connected to another XploWhatsApp workspace.",
            code="waba_in_use",
        )
    if data.phone_number_id:
        number_owner = await _number_owner(session, data.phone_number_id)
        if number_owner is not None and number_owner != tenant_id:
            raise Conflict("This phone number is connected to another workspace.", code="number_in_use")

    try:
        token = await graph.exchange_code(data.code)
    except GraphError as exc:
        raise meta_error(
            exc, "Meta did not accept the sign-up code. It expires after about 30 seconds; please try again."
        ) from exc

    try:
        info = await graph.debug_token(token)
    except GraphError as exc:
        raise meta_error(exc, "Could not verify the access Meta granted.") from exc
    settings = get_settings()
    if not info.is_valid or (info.app_id and info.app_id != settings.meta_app_id):
        raise ValidationFailed("Meta returned a token that is not valid for this app.", code="token_invalid")
    missing = [s for s in REQUIRED_SCOPES if s not in info.scopes]
    if missing:
        raise ValidationFailed(
            "Some WhatsApp permissions were not granted. "
            "Please run the connection again and allow all permissions.",
            code="scopes_missing",
            details={"missing": missing},
        )
    granted = info.targets_for("whatsapp_business_management")
    if granted and data.waba_id not in granted:
        raise ValidationFailed(
            "The selected WhatsApp account was not shared with XploWhatsApp.", code="waba_not_shared"
        )

    try:
        waba = await graph.get_waba(token, data.waba_id)
        phone_numbers = await graph.list_phone_numbers(token, data.waba_id)
    except GraphError as exc:
        raise meta_error(exc, "Could not read the WhatsApp Business Account from Meta.") from exc
    by_id = {str(p.get("id")): p for p in phone_numbers}
    if data.phone_number_id and data.phone_number_id not in by_id:
        raise ValidationFailed(
            "The selected phone number is not part of this WhatsApp account.", code="number_not_in_waba"
        )

    try:
        await graph.subscribe_app(token, data.waba_id)
    except GraphError as exc:
        raise meta_error(exc, "Could not subscribe XploWhatsApp to this account's webhooks.") from exc

    now = datetime.now(UTC)
    account = await account_by_waba_id(session, tenant_id, data.waba_id)
    if account is None:
        account = WhatsAppAccount(tenant_id=tenant_id, waba_id=data.waba_id)
        session.add(account)
    account.meta_business_id = data.business_id or (waba.get("owner_business_info") or {}).get("id")
    account.name = waba.get("name") or account.name
    account.currency = waba.get("currency")
    account.timezone_id = str(waba["timezone_id"]) if waba.get("timezone_id") else None
    account.message_template_namespace = waba.get("message_template_namespace")
    account.status = AccountStatus.CONNECTED
    account.health = AccountHealth.OK
    account.health_issues = []
    account.connected_by_user_id = user_id
    account.connected_at = now
    account.disconnected_at = None
    account.last_health_check_at = now
    await session.flush()
    await store_credential(
        session,
        account,
        CredentialKind.BUSINESS_TOKEN,
        token,
        scopes=info.scopes,
        expires_at=datetime.fromtimestamp(info.expires_at, UTC) if info.expires_at else None,
    )

    coexistence = (data.event or "").upper() == "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"
    existing = {n.phone_number_id: n for n in await numbers_for(session, account.id)}
    for pid, pdata in by_id.items():
        number = existing.get(pid)
        if number is None:
            number = WhatsAppNumber(tenant_id=tenant_id, account_id=account.id, phone_number_id=pid)
            session.add(number)
        _apply_number_fields(number, pdata)
        if number.status == NumberStatus.DISCONNECTED:
            number.status = NumberStatus.PENDING
    await session.flush()

    if data.phone_number_id:
        number = await number_by_phone_number_id(session, tenant_id, data.phone_number_id)
        assert number is not None
        number.is_coexistence = coexistence
        await _register_number(session, graph, account, number, token)
    else:
        add_health_issue(
            account,
            "no_phone_number",
            "No phone number was added during sign-up. Add one in WhatsApp Manager, then refresh.",
            level="warning",
        )
        account.health = AccountHealth.WARNING
    add_health_issue(
        account,
        "payment_method",
        "Add a payment method in WhatsApp Manager so Meta can bill your conversations.",
        level="info",
    )
    return account


async def _register_number(
    session: AsyncSession, graph: GraphClient, account: WhatsAppAccount, number: WhatsAppNumber, token: str
) -> None:
    """Register a number for Cloud API with a PIN we generate and keep encrypted.
    Coexistence numbers stay registered on the WhatsApp Business app, so they are
    marked connected without calling /register."""
    if number.is_coexistence:
        number.status = NumberStatus.CONNECTED
        number.registered_at = datetime.now(UTC)
        return
    pin = (
        await read_credential(session, account, CredentialKind.REGISTRATION_PIN, number.id) or generate_pin()
    )
    try:
        await graph.register_number(token, number.phone_number_id, pin)
    except GraphError as exc:
        number.status = NumberStatus.PENDING
        account.health = AccountHealth.WARNING
        add_health_issue(
            account,
            f"register_{number.phone_number_id}",
            f"Meta did not register {number.display_phone_number or 'the number'} for sending: {exc.message}",
            details=exc.public(),
        )
        return
    await store_credential(session, account, CredentialKind.REGISTRATION_PIN, pin, number_id=number.id)
    number.status = NumberStatus.CONNECTED
    number.registered_at = datetime.now(UTC)


async def retry_registration(session: AsyncSession, account: WhatsAppAccount, number: WhatsAppNumber) -> None:
    token = await business_token(session, account)
    async with GraphClient() as graph:
        await _register_number(session, graph, account, number, token)


# ---- health, disconnect, templates --------------------------------------------


async def check_health(
    session: AsyncSession, account: WhatsAppAccount, graph: GraphClient
) -> WhatsAppAccount:
    now = datetime.now(UTC)
    account.last_health_check_at = now
    if account.status == AccountStatus.DISCONNECTED:
        return account
    token = await read_credential(session, account, CredentialKind.BUSINESS_TOKEN)
    if token is None:
        account.status = AccountStatus.NEEDS_RECONNECT
        account.health = AccountHealth.ERROR
        add_health_issue(account, "token_missing", "Reconnect this account to restore access.")
        return account
    try:
        info = await graph.debug_token(token)
        if not info.is_valid:
            raise GraphError("Token is no longer valid.", code=190)
        apps = await graph.list_subscribed_apps(token, account.waba_id)
        phone_numbers = await graph.list_phone_numbers(token, account.waba_id)
    except GraphError as exc:
        if exc.is_token_error or exc.is_permission_error:
            account.status = AccountStatus.NEEDS_RECONNECT
            account.health = AccountHealth.ERROR
            add_health_issue(
                account,
                "token_invalid",
                "Meta no longer accepts XploWhatsApp's access to this account. "
                "Reconnect it to resume messaging.",
                details=exc.public(),
            )
        else:
            account.health = AccountHealth.WARNING
            add_health_issue(
                account,
                "health_check_failed",
                "Could not reach Meta for a health check.",
                level="warning",
                details=exc.public(),
            )
        return account

    # The check succeeded: drop issues it owns, then re-add whatever is still true.
    check_owned = (
        "token_",
        "health_check_failed",
        "webhooks_unsubscribed",
        "quality_",
        "status_",
        "number_removed_",
    )
    account.health_issues = [
        i for i in account.health_issues or [] if not str(i.get("code", "")).startswith(check_owned)
    ]
    problems: list[str] = []
    app_ids = {str((a.get("whatsapp_business_api_data") or {}).get("id") or a.get("id") or "") for a in apps}
    settings = get_settings()
    if settings.meta_app_id and settings.meta_app_id not in app_ids:
        try:
            await graph.subscribe_app(token, account.waba_id)
        except GraphError as exc:
            problems.append("webhooks")
            add_health_issue(
                account,
                "webhooks_unsubscribed",
                "Webhook subscription is missing and could not be restored.",
                details=exc.public(),
            )
    by_id = {str(p.get("id")): p for p in phone_numbers}
    for number in await numbers_for(session, account.id):
        pdata = by_id.get(number.phone_number_id)
        if pdata is None:
            number.status = NumberStatus.DISCONNECTED
            problems.append("number_removed")
            add_health_issue(
                account,
                f"number_removed_{number.phone_number_id}",
                f"{number.display_phone_number} is no longer in this WhatsApp account.",
                level="warning",
            )
            continue
        _apply_number_fields(number, pdata)
        if (number.quality_rating or "").upper() == "RED":
            problems.append("quality")
            add_health_issue(
                account,
                f"quality_{number.phone_number_id}",
                f"{number.display_phone_number} has a low quality rating. Marketing messages are paused.",
                level="warning",
            )
        if number.status == NumberStatus.CONNECTED and (number.meta_status or "").upper() in {
            "BANNED",
            "RESTRICTED",
            "FLAGGED",
        }:
            problems.append("number_status")
            add_health_issue(
                account,
                f"status_{number.phone_number_id}",
                f"Meta reports {number.display_phone_number} as {number.meta_status}.",
                level="warning",
            )
    account.status = AccountStatus.CONNECTED
    account.health = AccountHealth.WARNING if problems else AccountHealth.OK
    return account


async def mark_disconnected(session: AsyncSession, account: WhatsAppAccount) -> None:
    now = datetime.now(UTC)
    account.status = AccountStatus.DISCONNECTED
    account.health = AccountHealth.UNKNOWN
    account.disconnected_at = now
    await revoke_all_credentials(session, account)
    await session.execute(
        update(WhatsAppNumber)
        .where(WhatsAppNumber.account_id == account.id)
        .values(status=NumberStatus.DISCONNECTED)
    )


async def disconnect(session: AsyncSession, account: WhatsAppAccount) -> bool:
    """Unsubscribe from the WABA's webhooks, then forget credentials. History is kept.
    Returns False if Meta could not be told (access was probably already removed)."""
    told_meta = True
    token = await read_credential(session, account, CredentialKind.BUSINESS_TOKEN)
    if token:
        try:
            async with GraphClient() as graph:
                await graph.unsubscribe_app(token, account.waba_id)
        except GraphError as exc:
            told_meta = False
            log.info("disconnect_unsubscribe_failed", code=exc.code)
    else:
        told_meta = False
    await mark_disconnected(session, account)
    return told_meta


async def sync_templates(session: AsyncSession, account: WhatsAppAccount, graph: GraphClient) -> int:
    token = await business_token(session, account)
    now = datetime.now(UTC)
    existing = {
        (t.name, t.language): t
        for t in (
            await session.execute(select(MessageTemplate).where(MessageTemplate.account_id == account.id))
        ).scalars()
    }
    seen: set[tuple[str, str]] = set()
    count = 0
    try:
        async for item in graph.iter_templates(token, account.waba_id):
            key = (str(item.get("name")), str(item.get("language")))
            seen.add(key)
            template = existing.get(key)
            if template is None:
                template = MessageTemplate(
                    tenant_id=account.tenant_id, account_id=account.id, name=key[0], language=key[1]
                )
                session.add(template)
            template.meta_template_id = str(item.get("id")) if item.get("id") else None
            template.category = str(item.get("category") or "").upper()[:32] or None
            template.status = str(item.get("status") or "UNKNOWN").upper()[:32]
            reason = item.get("rejected_reason")
            template.rejected_reason = str(reason)[:200] if reason and reason != "NONE" else None
            template.components = list(item.get("components") or [])
            template.last_synced_at = now
            count += 1
    except GraphError as exc:
        if exc.is_token_error:
            account.status = AccountStatus.NEEDS_RECONNECT
            account.health = AccountHealth.ERROR
            add_health_issue(
                account,
                "token_invalid",
                "Meta no longer accepts XploWhatsApp's access. Reconnect to sync templates.",
            )
        raise meta_error(exc, "Could not fetch templates from Meta.") from exc
    for key, template in existing.items():
        if key not in seen:
            template.status = "DELETED"
    account.templates_synced_at = now
    return count
