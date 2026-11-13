"""Response shapes shared by the conversation, inbox and contact routers."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import User
from app.modules.conversations.models import Contact, Conversation, HandoverSession
from app.modules.conversations.service import service_window_open_at


class UserRef(BaseModel):
    id: uuid.UUID
    name: str


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    wa_id: str | None
    phone_e164: str | None
    bsuid: str | None
    username: str | None
    profile_name: str | None
    name: str | None
    language: str | None
    tags: list[str]
    consent_status: str
    consent_source: str | None
    consent_updated_at: datetime | None
    last_inbound_at: datetime | None
    created_at: datetime


class HandoverOut(BaseModel):
    started_by: str
    reason: str
    started_at: datetime
    agent: UserRef | None


class ConversationOut(BaseModel):
    id: uuid.UUID
    contact: ContactOut
    number_id: uuid.UUID
    number_display: str | None
    status: str
    unread_count: int
    last_inbound_at: datetime | None
    last_message_at: datetime | None
    service_window_expires_at: datetime | None
    service_window_open: bool
    last_message_preview: str | None
    ai_enabled: bool
    needs_human: bool
    awaiting_reply: bool
    assigned_to: UserRef | None
    assigned_at: datetime | None
    closed_at: datetime | None
    handover: HandoverOut | None


async def user_names(session: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    rows = await session.execute(select(User.id, User.full_name).where(User.id.in_(wanted)))
    return {uid: name for uid, name in rows.all()}


def user_ref(user_id: uuid.UUID | None, names: dict[uuid.UUID, str]) -> UserRef | None:
    if user_id is None:
        return None
    return UserRef(id=user_id, name=names.get(user_id, "Former member"))


async def conversations_out(
    session: AsyncSession,
    rows: Sequence[tuple[Conversation, Contact, str | None, str | None]],
    now: datetime,
) -> list[ConversationOut]:
    """Build list items with assignee names and the active handover, in two queries."""
    ids = [c.id for c, *_ in rows]
    handovers: dict[uuid.UUID, HandoverSession] = {}
    if ids:
        found = await session.execute(
            select(HandoverSession).where(
                HandoverSession.conversation_id.in_(ids), HandoverSession.released_at.is_(None)
            )
        )
        handovers = {h.conversation_id: h for h in found.scalars()}
    names = await user_names(
        session,
        [c.assigned_user_id for c, *_ in rows] + [h.agent_user_id for h in handovers.values()],
    )
    out = []
    for conv, contact, display, preview in rows:
        h = handovers.get(conv.id)
        out.append(
            ConversationOut(
                id=conv.id,
                contact=ContactOut.model_validate(contact),
                number_id=conv.number_id,
                number_display=display,
                status=conv.status,
                unread_count=conv.unread_count,
                last_inbound_at=conv.last_inbound_at,
                last_message_at=conv.last_message_at,
                service_window_expires_at=conv.service_window_expires_at,
                service_window_open=service_window_open_at(conv, now),
                last_message_preview=(preview or "")[:120] or None,
                ai_enabled=conv.ai_enabled,
                needs_human=conv.needs_human,
                awaiting_reply=conv.awaiting_reply,
                assigned_to=user_ref(conv.assigned_user_id, names),
                assigned_at=conv.assigned_at,
                closed_at=conv.closed_at,
                handover=HandoverOut(
                    started_by=h.started_by,
                    reason=h.reason,
                    started_at=h.started_at,
                    agent=user_ref(h.agent_user_id, names),
                )
                if h
                else None,
            )
        )
    return out
