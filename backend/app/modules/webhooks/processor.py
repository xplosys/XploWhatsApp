"""Worker-side processing of stored webhook events, one event per transaction."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import app_transaction
from app.modules.conversations import service as conv
from app.modules.webhooks.models import WebhookEvent
from app.modules.whatsapp import service as wa
from app.modules.whatsapp.models import (
    AccountHealth,
    AccountStatus,
    MessageTemplate,
    WhatsAppAccount,
    WhatsAppNumber,
)

log = structlog.get_logger(__name__)


class RetryLater(Exception):
    """The event depends on data that is not there yet (for example a status update
    that arrives before the send result is committed)."""


def _ts(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


_MEDIA_TYPES = {"image", "audio", "video", "document", "sticker"}


def summarise(message: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    """(type, readable body, payload to store) for an inbound message object."""
    mtype = str(message.get("type") or "unknown")
    raw_obj = message.get(mtype)
    obj: dict[str, Any] = raw_obj if isinstance(raw_obj, dict) else {}
    body: str | None = None
    if mtype == "text":
        body = obj.get("body")
    elif mtype in _MEDIA_TYPES:
        body = obj.get("caption") or obj.get("filename")
    elif mtype == "button":
        body = obj.get("text")
    elif mtype == "interactive":
        reply = obj.get("button_reply") or obj.get("list_reply") or {}
        body = reply.get("title") or (obj.get("nfm_reply") or {}).get("body")
    elif mtype == "reaction":
        body = obj.get("emoji")
    elif mtype == "location":
        body = obj.get("name") or obj.get("address") or f"{obj.get('latitude')},{obj.get('longitude')}"
    elif mtype == "contacts":
        names = [((c.get("name") or {}).get("formatted_name")) for c in message.get("contacts") or []]
        body = ", ".join(n for n in names if n) or None
        obj = {"contacts": message.get("contacts") or []}
    elif mtype == "order":
        body = obj.get("text")
    elif mtype == "unsupported":
        body = None
    payload = {"type": mtype, mtype: obj} if obj else {"type": mtype}
    if message.get("errors"):
        payload["errors"] = message["errors"]
    if message.get("referral"):
        payload["referral"] = message["referral"]
    return mtype, body, payload


async def _handle_messages(session: AsyncSession, tenant_id: uuid.UUID, value: dict[str, Any]) -> None:
    phone_number_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
    number = await wa.number_by_phone_number_id(session, tenant_id, phone_number_id)
    if number is None:
        log.info("webhook_number_not_found", phone_number_id=phone_number_id)
        return

    profiles: dict[str, dict[str, Any]] = {}
    for c in value.get("contacts") or []:
        for key in (c.get("user_id"), c.get("wa_id")):
            if key:
                profiles[str(key)] = c

    for message in value.get("messages") or []:
        mtype = message.get("type")
        if mtype == "system":
            system = message.get("system") or {}
            if system.get("type") == "user_changed_number":
                await conv.change_identifier(
                    session, tenant_id, old_wa_id=message.get("from"), new_wa_id=system.get("wa_id")
                )
            elif system.get("type") == "user_changed_user_id":
                await conv.change_identifier(
                    session,
                    tenant_id,
                    old_bsuid=system.get("previous_user_id"),
                    new_bsuid=system.get("user_id"),
                    new_parent_bsuid=system.get("parent_user_id"),
                )
            continue
        wa_id = message.get("from")
        bsuid = message.get("from_user_id")
        profile = profiles.get(str(bsuid)) or profiles.get(str(wa_id)) or {}
        if not wa_id and not bsuid:
            log.warning("webhook_message_without_sender", wa_message_id=message.get("id"))
            continue
        contact = await conv.upsert_contact(
            session,
            tenant_id,
            wa_id=wa_id or profile.get("wa_id"),
            bsuid=bsuid or profile.get("user_id"),
            parent_bsuid=message.get("from_parent_user_id") or profile.get("parent_user_id"),
            username=(profile.get("profile") or {}).get("username") or profile.get("username"),
            profile_name=(profile.get("profile") or {}).get("name"),
        )
        msg_type, body, payload = summarise(message)
        await conv.record_inbound(
            session,
            tenant_id,
            contact=contact,
            number_id=number.id,
            wa_message_id=str(message["id"]),
            msg_type=msg_type,
            body=body,
            payload=payload,
            wa_timestamp=_ts(message.get("timestamp")),
            context_wa_message_id=(message.get("context") or {}).get("id"),
        )

    for status in value.get("statuses") or []:
        wa_message_id = str(status.get("id") or "")
        msg = await conv.message_by_wa_id(session, tenant_id, wa_message_id)
        if msg is None:
            # The send result may not be committed yet; retry a few times.
            raise RetryLater(f"message {wa_message_id} not found for status {status.get('status')}")
        conv.apply_status(
            msg,
            status=str(status.get("status")),
            at=_ts(status.get("timestamp")),
            errors=status.get("errors"),
            pricing=status.get("pricing"),
        )

    if value.get("errors"):
        log.warning("webhook_value_errors", errors=value.get("errors"))


async def _handle_template_status(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID | None, value: dict[str, Any]
) -> None:
    meta_id = str(value.get("message_template_id") or "")
    template = None
    if meta_id:
        template = (
            await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.tenant_id == tenant_id, MessageTemplate.meta_template_id == meta_id
                )
            )
        ).scalar_one_or_none()
    if template is None and account_id and value.get("message_template_name"):
        template = (
            await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.account_id == account_id,
                    MessageTemplate.name == value["message_template_name"],
                    MessageTemplate.language == value.get("message_template_language"),
                )
            )
        ).scalar_one_or_none()
    if template is None:
        return  # picked up by the next sync
    event = str(value.get("event") or "").upper()
    if event:
        template.status = event[:32]
    reason = value.get("reason")
    template.rejected_reason = str(reason)[:200] if reason and reason != "NONE" else None


async def _handle_template_category(
    session: AsyncSession, tenant_id: uuid.UUID, value: dict[str, Any]
) -> None:
    meta_id = str(value.get("message_template_id") or "")
    if not meta_id or not value.get("new_category"):
        return
    template = (
        await session.execute(
            select(MessageTemplate).where(
                MessageTemplate.tenant_id == tenant_id, MessageTemplate.meta_template_id == meta_id
            )
        )
    ).scalar_one_or_none()
    if template:
        template.category = str(value["new_category"]).upper()[:32]


async def _handle_quality(session: AsyncSession, account: WhatsAppAccount, value: dict[str, Any]) -> None:
    display = conv.normalise_wa_id(value.get("display_phone_number"))
    numbers = (
        await session.execute(select(WhatsAppNumber).where(WhatsAppNumber.account_id == account.id))
    ).scalars()
    for number in numbers:
        if conv.normalise_wa_id(number.display_phone_number) == display:
            if value.get("current_limit"):
                number.messaging_limit_tier = str(value["current_limit"])[:32]
            event = str(value.get("event") or "")
            # FLAGGED means low quality; UPGRADE/DOWNGRADE only move the messaging limit.
            # After UNFLAGGED the exact rating is unknown until the next health check.
            if event == "FLAGGED":
                number.quality_rating = "RED"
            elif event == "UNFLAGGED":
                number.quality_rating = None
            wa.add_health_issue(
                account,
                "quality_update",
                f"Quality event {event} for {number.display_phone_number}",
                level="info",
            )


async def _handle_account_update(
    session: AsyncSession, account: WhatsAppAccount, value: dict[str, Any]
) -> None:
    event = str(value.get("event") or "").upper()
    if event == "PARTNER_APP_UNINSTALLED":
        account.status = AccountStatus.NEEDS_RECONNECT
        account.health = AccountHealth.ERROR
        wa.add_health_issue(account, "app_uninstalled", "The business removed XploWhatsApp's access in Meta.")
    elif event == "ACCOUNT_DELETED":
        await wa.mark_disconnected(session, account)
        wa.add_health_issue(
            account, "account_deleted", "Meta reports that this WhatsApp account was deleted."
        )
    elif event in {"DISABLED_UPDATE", "ACCOUNT_VIOLATION", "ACCOUNT_RESTRICTION"}:
        account.health = AccountHealth.WARNING
        detail = value.get("violation_info") or value.get("ban_info") or value.get("restriction_info") or {}
        wa.add_health_issue(
            account, event.lower(), f"Meta reported {event.replace('_', ' ').lower()}.", details=detail
        )
    elif event:
        wa.add_health_issue(account, event.lower(), f"Meta account update: {event}.", level="info")


async def process_event(event_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """Process one stored event. Returns the resulting status."""
    try:
        async with app_transaction(tenant_id) as session:
            event = (
                await session.execute(
                    select(WebhookEvent).where(WebhookEvent.id == event_id).with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if event is None or event.status != "pending":
                return event.status if event else "missing"
            value = event.payload.get("value") or {}
            account = (
                await wa.account_by_waba_id(session, tenant_id, event.waba_id) if event.waba_id else None
            )
            handled = True
            if event.field == "messages":
                await _handle_messages(session, tenant_id, value)
            elif event.field == "message_template_status_update":
                await _handle_template_status(session, tenant_id, account.id if account else None, value)
            elif event.field == "template_category_update":
                await _handle_template_category(session, tenant_id, value)
            elif event.field == "phone_number_quality_update" and account:
                await _handle_quality(session, account, value)
            elif event.field == "account_update" and account:
                await _handle_account_update(session, account, value)
            else:
                handled = False
            event.status = "processed" if handled else "ignored"
            event.processed_at = datetime.now(UTC)
            event.attempts += 1
            return event.status
    except Exception as exc:
        await _record_failure(event_id, tenant_id, exc)
        if not isinstance(exc, RetryLater):
            log.exception("webhook_event_failed", event_id=str(event_id))
        return "retry"


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(5 * 2 ** max(attempts - 1, 0), 3600))


async def _record_failure(event_id: uuid.UUID, tenant_id: uuid.UUID, exc: Exception) -> None:
    async with app_transaction(tenant_id) as session:
        event = await session.get(WebhookEvent, event_id)
        if event is None:
            return
        event.attempts += 1
        event.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        if event.attempts >= get_settings().webhook_max_attempts:
            event.status = "failed"
        else:
            event.next_attempt_at = datetime.now(UTC) + backoff(event.attempts)
