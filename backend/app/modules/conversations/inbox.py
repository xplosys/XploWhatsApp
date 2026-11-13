"""The agent workflow on a conversation: assignment, taking over from the assistant and
handing back, closing, internal notes, contact details and tags, and the record of
questions the assistant could not answer."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.modules.conversations.models import (
    Contact,
    Conversation,
    ConversationNote,
    ConversationSummary,
    HandoverSession,
    HandoverStarter,
    SummaryStatus,
    UnansweredQuestion,
)
from app.modules.tenants.models import TenantMembership

MAX_TAGS = 20
_TAG = re.compile(r"^[\w][\w &+./-]{0,39}$", re.UNICODE)


async def active_member(session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return (
        await session.scalar(
            select(TenantMembership.id).where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.user_id == user_id,
                TenantMembership.status == "active",
            )
        )
    ) is not None


async def get_conversation(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID, *, lock: bool = False
) -> Conversation:
    conv = await session.get(Conversation, conversation_id, with_for_update=lock)
    if conv is None or conv.tenant_id != tenant_id:
        raise NotFound("Conversation not found.", code="conversation_not_found")
    return conv


# ---- assignment ---------------------------------------------------------------------


async def assign(
    session: AsyncSession,
    conv: Conversation,
    *,
    actor_id: uuid.UUID,
    can_assign_others: bool,
    user_id: uuid.UUID | None,
) -> bool:
    """Assign to a member, or unassign (None). Without the assign permission a person
    may only pick up an unassigned conversation or let go of their own. Returns
    whether anything changed."""
    if not can_assign_others:
        taking = user_id == actor_id and conv.assigned_user_id in (None, actor_id)
        letting_go = user_id is None and conv.assigned_user_id == actor_id
        if not (taking or letting_go):
            raise Forbidden(
                "You can pick up unassigned conversations or let go of your own. "
                "A supervisor or admin can reassign others.",
                code="assignment_not_allowed",
            )
    if user_id is not None and not await active_member(session, conv.tenant_id, user_id):
        raise ValidationFailed("That person is not an active member of this business.", code="not_a_member")
    if conv.assigned_user_id == user_id:
        return False
    conv.assigned_user_id = user_id
    conv.assigned_at = datetime.now(UTC) if user_id else None
    handover = await active_handover(session, conv.id)
    if handover is not None and user_id is not None:
        handover.agent_user_id = user_id
    return True


async def unassign_member(session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """When someone leaves a business their conversations, open takeovers and leads go
    back to the shared queue."""
    from app.modules.leads.models import Lead

    await session.execute(
        update(Conversation)
        .where(Conversation.tenant_id == tenant_id, Conversation.assigned_user_id == user_id)
        .values(assigned_user_id=None, assigned_at=None)
    )
    await session.execute(
        update(HandoverSession)
        .where(
            HandoverSession.tenant_id == tenant_id,
            HandoverSession.agent_user_id == user_id,
            HandoverSession.released_at.is_(None),
        )
        .values(agent_user_id=None)
    )
    await session.execute(
        update(Lead)
        .where(Lead.tenant_id == tenant_id, Lead.owner_user_id == user_id)
        .values(owner_user_id=None)
    )


# ---- handover -----------------------------------------------------------------------


async def active_handover(session: AsyncSession, conversation_id: uuid.UUID) -> HandoverSession | None:
    return (
        await session.execute(
            select(HandoverSession).where(
                HandoverSession.conversation_id == conversation_id, HandoverSession.released_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def start_handover(
    session: AsyncSession,
    conv: Conversation,
    *,
    started_by: HandoverStarter,
    reason: str,
    agent_user_id: uuid.UUID | None = None,
) -> HandoverSession:
    """Stop automatic replies. When the assistant asks for help the conversation is
    marked as needing a person; when a person takes over, it is already being handled."""
    conv.ai_enabled = False
    conv.needs_human = started_by != HandoverStarter.AGENT
    current = await active_handover(session, conv.id)
    if current is not None:
        if agent_user_id is not None:
            current.agent_user_id = agent_user_id
        if started_by == HandoverStarter.AGENT:
            conv.needs_human = False
        return current
    handover = HandoverSession(
        tenant_id=conv.tenant_id,
        conversation_id=conv.id,
        started_by=started_by,
        reason=reason[:64],
        agent_user_id=agent_user_id,
        started_at=datetime.now(UTC),
    )
    session.add(handover)
    await session.flush()
    return handover


async def release_handover(session: AsyncSession, conv: Conversation, *, user_id: uuid.UUID | None) -> None:
    """Give the conversation back to the assistant."""
    conv.ai_enabled = True
    conv.needs_human = False
    current = await active_handover(session, conv.id)
    if current is not None:
        current.released_at = datetime.now(UTC)
        current.released_by_user_id = user_id


# ---- status ---------------------------------------------------------------------------


def set_status(conv: Conversation, status: str) -> bool:
    if conv.status == status:
        return False
    conv.status = status
    conv.closed_at = datetime.now(UTC) if status == "closed" else None
    if status == "closed":
        conv.unread_count = 0
    return True


# ---- notes ----------------------------------------------------------------------------


async def add_note(
    session: AsyncSession, conv: Conversation, author_id: uuid.UUID, body: str
) -> ConversationNote:
    text = body.strip()
    if not text:
        raise ValidationFailed("The note is empty.", code="empty_note")
    note = ConversationNote(
        tenant_id=conv.tenant_id, conversation_id=conv.id, author_user_id=author_id, body=text
    )
    session.add(note)
    await session.flush()
    await session.refresh(note)
    return note


async def get_note(session: AsyncSession, tenant_id: uuid.UUID, note_id: uuid.UUID) -> ConversationNote:
    note = await session.get(ConversationNote, note_id)
    if note is None or note.tenant_id != tenant_id:
        raise NotFound("Note not found.", code="note_not_found")
    return note


# ---- contacts and tags -------------------------------------------------------------


def clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for raw in tags:
        tag = " ".join(raw.strip().lower().split())
        if not tag:
            continue
        if not _TAG.match(tag):
            raise ValidationFailed(
                f'"{raw[:40]}" is not a valid tag. Use up to 40 letters, numbers, spaces or - _ . & + /.',
                code="invalid_tag",
            )
        if tag not in out:
            out.append(tag)
    if len(out) > MAX_TAGS:
        raise ValidationFailed(f"A contact can have at most {MAX_TAGS} tags.", code="too_many_tags")
    return out


async def get_contact(session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> Contact:
    contact = await session.get(Contact, contact_id)
    if contact is None or contact.tenant_id != tenant_id:
        raise NotFound("Contact not found.", code="contact_not_found")
    return contact


# ---- summaries ------------------------------------------------------------------------


async def request_summary(
    session: AsyncSession, conv: Conversation, user_id: uuid.UUID | None
) -> ConversationSummary:
    """Mark the summary as queued (the caller dispatches the job after commit)."""
    row = (
        await session.execute(
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conv.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = ConversationSummary(tenant_id=conv.tenant_id, conversation_id=conv.id)
        session.add(row)
    row.status = SummaryStatus.QUEUED
    row.error = None
    row.requested_by_user_id = user_id
    await session.flush()
    await session.refresh(row)
    return row


# ---- unanswered questions -----------------------------------------------------------


async def record_unanswered(
    session: AsyncSession,
    conv: Conversation,
    *,
    message_id: uuid.UUID,
    question: str,
    reason: str,
    retrieval_hits: int | None,
) -> None:
    exists = await session.scalar(
        select(UnansweredQuestion.id).where(UnansweredQuestion.message_id == message_id)
    )
    if exists is not None:
        return
    session.add(
        UnansweredQuestion(
            tenant_id=conv.tenant_id,
            conversation_id=conv.id,
            message_id=message_id,
            question=question[:2000],
            reason=reason[:64],
            retrieval_hits=retrieval_hits,
        )
    )
