"""Contacts, conversations and messages: identity matching and timeline writes.

All functions run inside a tenant-bound transaction (RLS applies).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.conversations.models import (
    STATUS_RANK,
    ConsentStatus,
    Contact,
    Conversation,
    Direction,
    Message,
    MessageStatus,
    SenderType,
)

_DIGITS = re.compile(r"\D+")


def normalise_wa_id(value: str | None) -> str | None:
    """WhatsApp IDs are phone numbers in international format with digits only."""
    if not value:
        return None
    digits = _DIGITS.sub("", value)
    return digits or None


def e164(wa_id: str | None) -> str | None:
    return f"+{wa_id}" if wa_id else None


async def find_contact(
    session: AsyncSession, tenant_id: uuid.UUID, *, wa_id: str | None, bsuid: str | None
) -> Contact | None:
    """Match order: BSUID first (stable per business portfolio), then phone."""
    if bsuid:
        found = (
            await session.execute(
                select(Contact).where(Contact.tenant_id == tenant_id, Contact.bsuid == bsuid)
            )
        ).scalar_one_or_none()
        if found:
            return found
    if wa_id:
        return (
            await session.execute(
                select(Contact).where(Contact.tenant_id == tenant_id, Contact.wa_id == wa_id)
            )
        ).scalar_one_or_none()
    return None


async def upsert_contact(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    wa_id: str | None,
    bsuid: str | None = None,
    parent_bsuid: str | None = None,
    username: str | None = None,
    profile_name: str | None = None,
) -> Contact:
    wa_id = normalise_wa_id(wa_id)
    if not wa_id and not bsuid:
        raise ValueError("A contact needs a phone number or a business-scoped user ID.")
    contact = await find_contact(session, tenant_id, wa_id=wa_id, bsuid=bsuid)
    if contact is None:
        contact = Contact(tenant_id=tenant_id, consent_status=ConsentStatus.UNKNOWN)
        session.add(contact)
    # Fill identifiers we did not have; never overwrite a known phone with nothing.
    if wa_id and contact.wa_id != wa_id:
        if contact.wa_id is None or await find_contact(session, tenant_id, wa_id=wa_id, bsuid=None) is None:
            contact.wa_id = wa_id
            contact.phone_e164 = e164(wa_id)
    if bsuid and not contact.bsuid:
        contact.bsuid = bsuid
    if parent_bsuid:
        contact.parent_bsuid = parent_bsuid
    if username:
        contact.username = username[:64]
    if profile_name:
        contact.profile_name = profile_name[:200]
    await session.flush()
    return contact


async def get_or_create_conversation(
    session: AsyncSession, tenant_id: uuid.UUID, contact: Contact, number_id: uuid.UUID
) -> Conversation:
    conv = (
        await session.execute(
            select(Conversation).where(
                Conversation.tenant_id == tenant_id,
                Conversation.contact_id == contact.id,
                Conversation.number_id == number_id,
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        conv = Conversation(tenant_id=tenant_id, contact_id=contact.id, number_id=number_id, status="open")
        session.add(conv)
        await session.flush()
    return conv


async def message_by_wa_id(session: AsyncSession, tenant_id: uuid.UUID, wa_message_id: str) -> Message | None:
    return (
        await session.execute(
            select(Message).where(Message.tenant_id == tenant_id, Message.wa_message_id == wa_message_id)
        )
    ).scalar_one_or_none()


def _consent_keyword(body: str | None) -> str | None:
    if not body:
        return None
    normalised = " ".join(body.strip().lower().split())
    settings = get_settings()
    if normalised in {k.lower() for k in settings.whatsapp_opt_out_keywords}:
        return ConsentStatus.OPTED_OUT
    if normalised in {k.lower() for k in settings.whatsapp_opt_in_keywords}:
        return ConsentStatus.OPTED_IN
    return None


def set_consent(contact: Contact, status: str, source: str, now: datetime | None = None) -> None:
    contact.consent_status = status
    contact.consent_source = source[:64]
    contact.consent_updated_at = now or datetime.now(UTC)


async def record_inbound(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    contact: Contact,
    number_id: uuid.UUID,
    wa_message_id: str,
    msg_type: str,
    body: str | None,
    payload: dict[str, Any],
    wa_timestamp: datetime,
    context_wa_message_id: str | None = None,
) -> Message | None:
    """Store an inbound message once. Returns None for a duplicate delivery."""
    if await message_by_wa_id(session, tenant_id, wa_message_id) is not None:
        return None
    conv = await get_or_create_conversation(session, tenant_id, contact, number_id)
    window = timedelta(hours=get_settings().whatsapp_service_window_hours)
    if conv.last_inbound_at is None or wa_timestamp >= conv.last_inbound_at:
        conv.last_inbound_at = wa_timestamp
        conv.service_window_expires_at = wa_timestamp + window
    conv.last_message_at = max(conv.last_message_at or wa_timestamp, wa_timestamp)
    conv.unread_count = (conv.unread_count or 0) + 1
    conv.status = "open"
    if contact.last_inbound_at is None or wa_timestamp > contact.last_inbound_at:
        contact.last_inbound_at = wa_timestamp

    keyword = _consent_keyword(body) if msg_type == "text" else None
    if keyword:
        set_consent(contact, keyword, "whatsapp_keyword")

    msg = Message(
        tenant_id=tenant_id,
        conversation_id=conv.id,
        direction=Direction.INBOUND,
        sender_type=SenderType.CONTACT,
        type=msg_type[:32],
        body=body,
        payload=payload,
        wa_message_id=wa_message_id,
        context_wa_message_id=context_wa_message_id,
        status=MessageStatus.RECEIVED,
        wa_timestamp=wa_timestamp,
    )
    session.add(msg)
    await session.flush()
    return msg


def apply_status(
    msg: Message,
    *,
    status: str,
    at: datetime,
    errors: list[dict[str, Any]] | None = None,
    pricing: dict[str, Any] | None = None,
) -> bool:
    """Apply a Meta status update without ever moving backwards. Returns True if changed."""
    changed = False
    if pricing:
        msg.pricing_category = str(pricing.get("category") or "")[:32] or msg.pricing_category
        if "billable" in pricing:
            msg.billable = bool(pricing["billable"])
        changed = True
    if status == MessageStatus.FAILED:
        if msg.status != MessageStatus.FAILED:
            first = (errors or [{}])[0]
            msg.status = MessageStatus.FAILED
            msg.error_code = str(first.get("code") or "")[:32] or None
            details = (first.get("error_data") or {}).get("details") if isinstance(first, dict) else None
            msg.error_message = str(details or first.get("title") or first.get("message") or "Failed")[:500]
            msg.status_updated_at = at
            return True
        return changed
    if status not in STATUS_RANK or msg.status == MessageStatus.FAILED:
        return changed
    if STATUS_RANK[status] > STATUS_RANK.get(msg.status, -1):
        msg.status = status
        msg.status_updated_at = at
        return True
    return changed


async def change_identifier(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    old_wa_id: str | None = None,
    new_wa_id: str | None = None,
    old_bsuid: str | None = None,
    new_bsuid: str | None = None,
    new_parent_bsuid: str | None = None,
) -> Contact | None:
    """Handle user_changed_number / user_changed_user_id system messages: move the
    identifier on the existing contact and keep the old one for history."""
    old_wa_id = normalise_wa_id(old_wa_id)
    new_wa_id = normalise_wa_id(new_wa_id)
    conditions = []
    if old_bsuid:
        conditions.append(Contact.bsuid == old_bsuid)
    if old_wa_id:
        conditions.append(Contact.wa_id == old_wa_id)
    if not conditions:
        return None
    contact = (
        await session.execute(
            select(Contact).where(Contact.tenant_id == tenant_id, or_(*conditions)).limit(1)
        )
    ).scalar_one_or_none()
    if contact is None:
        return None
    history = list(contact.previous_identifiers or [])
    history.append(
        {"wa_id": contact.wa_id, "bsuid": contact.bsuid, "changed_at": datetime.now(UTC).isoformat()}
    )
    contact.previous_identifiers = history[-20:]
    if new_wa_id and await find_contact(session, tenant_id, wa_id=new_wa_id, bsuid=None) is None:
        contact.wa_id = new_wa_id
        contact.phone_e164 = e164(new_wa_id)
    if new_bsuid and await find_contact(session, tenant_id, wa_id=None, bsuid=new_bsuid) is None:
        contact.bsuid = new_bsuid
    if new_parent_bsuid:
        contact.parent_bsuid = new_parent_bsuid
    await session.flush()
    return contact


def service_window_open_at(conversation: Conversation, now: datetime) -> bool:
    return bool(conversation.service_window_expires_at and conversation.service_window_expires_at > now)
