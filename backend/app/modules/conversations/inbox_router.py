"""The agent workflow on one conversation: assignment, taking over from the assistant
and handing back, closing and reopening, internal notes, and the AI summary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import Meta, Requires, TenantContext
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.db.session import run_after_commit
from app.modules.audit.service import record
from app.modules.conversations import inbox
from app.modules.conversations.models import (
    ConversationNote,
    ConversationSummary,
    HandoverSession,
    HandoverStarter,
    Message,
    SummaryStatus,
)
from app.modules.conversations.router import single_out
from app.modules.conversations.schemas import ConversationOut, UserRef, user_names, user_ref
from app.modules.rbac.permissions import Permission
from app.workers.dispatch import CONVERSATION_SUMMARY, dispatch

router = APIRouter(tags=["inbox"])
Reader = Annotated[TenantContext, Requires(Permission.CONVERSATIONS_READ)]
Replier = Annotated[TenantContext, Requires(Permission.CONVERSATIONS_REPLY)]

NOTE_MAX_CHARS = 4000
# A summary request already queued this recently is not sent to the worker again.
SUMMARY_REQUEUE_AFTER = timedelta(minutes=2)


# ---- assignment, takeover, status ---------------------------------------------------


class AssigneeUpdate(BaseModel):
    user_id: uuid.UUID | None = Field(description="The member to assign, or null to unassign.")


class StatusUpdate(BaseModel):
    status: Literal["open", "closed"]


@router.put("/conversations/{conversation_id}/assignee", response_model=ConversationOut)
async def set_assignee(
    conversation_id: uuid.UUID, body: AssigneeUpdate, ctx: Replier, meta: Meta
) -> ConversationOut:
    """Assign or unassign. Agents may pick up an unassigned conversation or let go of
    their own; supervisors, admins and owners may assign anyone."""
    conv = await inbox.get_conversation(ctx.session, ctx.tenant_id, conversation_id, lock=True)
    previous = conv.assigned_user_id
    changed = await inbox.assign(
        ctx.session,
        conv,
        actor_id=ctx.user.id,
        can_assign_others=ctx.can(Permission.CONVERSATIONS_ASSIGN),
        user_id=body.user_id,
    )
    if changed:
        await record(
            ctx.session,
            "conversation.assigned" if body.user_id else "conversation.unassigned",
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="conversation",
            target_id=conv.id,
            details={
                "from": str(previous) if previous else None,
                "to": str(body.user_id) if body.user_id else None,
            },
        )
    return await single_out(ctx, conv)


@router.post("/conversations/{conversation_id}/takeover", response_model=ConversationOut)
async def take_over(conversation_id: uuid.UUID, ctx: Replier, meta: Meta) -> ConversationOut:
    """Stop the assistant and answer this conversation yourself. An unassigned
    conversation is assigned to you."""
    conv = await inbox.get_conversation(ctx.session, ctx.tenant_id, conversation_id, lock=True)
    if conv.assigned_user_id is None:
        await inbox.assign(
            ctx.session, conv, actor_id=ctx.user.id, can_assign_others=False, user_id=ctx.user.id
        )
    await inbox.start_handover(
        ctx.session,
        conv,
        started_by=HandoverStarter.AGENT,
        reason="agent_takeover",
        agent_user_id=ctx.user.id,
    )
    await record(
        ctx.session,
        "conversation.taken_over",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="conversation",
        target_id=conv.id,
    )
    return await single_out(ctx, conv)


@router.post("/conversations/{conversation_id}/release", response_model=ConversationOut)
async def release(conversation_id: uuid.UUID, ctx: Replier, meta: Meta) -> ConversationOut:
    """Hand the conversation back to the assistant. It answers the customer's next message."""
    conv = await inbox.get_conversation(ctx.session, ctx.tenant_id, conversation_id, lock=True)
    await inbox.release_handover(ctx.session, conv, user_id=ctx.user.id)
    await record(
        ctx.session,
        "conversation.released",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="conversation",
        target_id=conv.id,
    )
    return await single_out(ctx, conv)


@router.put("/conversations/{conversation_id}/status", response_model=ConversationOut)
async def set_status(
    conversation_id: uuid.UUID, body: StatusUpdate, ctx: Replier, meta: Meta
) -> ConversationOut:
    """Close or reopen. A closed conversation reopens by itself when the customer writes."""
    conv = await inbox.get_conversation(ctx.session, ctx.tenant_id, conversation_id, lock=True)
    if inbox.set_status(conv, body.status):
        await record(
            ctx.session,
            "conversation.closed" if body.status == "closed" else "conversation.reopened",
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="conversation",
            target_id=conv.id,
        )
    return await single_out(ctx, conv)


class HandoverHistoryOut(BaseModel):
    id: uuid.UUID
    started_by: str
    reason: str
    started_at: datetime
    agent: UserRef | None
    released_at: datetime | None
    released_by: UserRef | None


@router.get("/conversations/{conversation_id}/handovers", response_model=list[HandoverHistoryOut])
async def handover_history(conversation_id: uuid.UUID, ctx: Reader) -> list[HandoverHistoryOut]:
    """When people (rather than the assistant) answered this conversation, newest first."""
    conv = await inbox.get_conversation(ctx.session, ctx.tenant_id, conversation_id)
    rows = list(
        (
            await ctx.session.execute(
                select(HandoverSession)
                .where(HandoverSession.conversation_id == conv.id)
                .order_by(HandoverSession.started_at.desc())
                .limit(50)
            )
        ).scalars()
    )
    names = await user_names(
        ctx.session, [h.agent_user_id for h in rows] + [h.released_by_user_id for h in rows]
    )
    return [
        HandoverHistoryOut(
            id=h.id,
            started_by=h.started_by,
            reason=h.reason,
            started_at=h.started_at,
            agent=user_ref(h.agent_user_id, names),
            released_at=h.released_at,
            released_by=user_ref(h.released_by_user_id, names),
        )
        for h in rows
    ]


# ---- notes ----------------------------------------------------------------------------


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=NOTE_MAX_CHARS)


class NoteOut(BaseModel):
    id: uuid.UUID
    body: str
    author: UserRef | None
    created_at: datetime
    updated_at: datetime
    can_edit: bool
    can_delete: bool


def _note_out(ctx: TenantContext, note: ConversationNote, names: dict[uuid.UUID, str]) -> NoteOut:
    mine = note.author_user_id == ctx.user.id
    return NoteOut(
        id=note.id,
        body=note.body,
        author=user_ref(note.author_user_id, names),
        created_at=note.created_at,
        updated_at=note.updated_at,
        can_edit=mine,
        can_delete=mine or ctx.can(Permission.CONVERSATIONS_ASSIGN),
    )


@router.get("/conversations/{conversation_id}/notes", response_model=list[NoteOut])
async def list_notes(conversation_id: uuid.UUID, ctx: Reader) -> list[NoteOut]:
    """Internal notes, oldest first. Customers never see them."""
    conv = await inbox.get_conversation(ctx.session, ctx.tenant_id, conversation_id)
    notes = list(
        (
            await ctx.session.execute(
                select(ConversationNote)
                .where(ConversationNote.conversation_id == conv.id)
                .order_by(ConversationNote.created_at)
                .limit(500)
            )
        ).scalars()
    )
    names = await user_names(ctx.session, [n.author_user_id for n in notes])
    return [_note_out(ctx, n, names) for n in notes]


@router.post("/conversations/{conversation_id}/notes", response_model=NoteOut, status_code=201)
async def add_note(conversation_id: uuid.UUID, body: NoteIn, ctx: Replier, meta: Meta) -> NoteOut:
    conv = await inbox.get_conversation(ctx.session, ctx.tenant_id, conversation_id)
    note = await inbox.add_note(ctx.session, conv, ctx.user.id, body.body)
    await record(
        ctx.session,
        "conversation.note_added",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="conversation_note",
        target_id=note.id,
        details={"conversation_id": str(conversation_id)},
    )
    return _note_out(ctx, note, {ctx.user.id: ctx.user.full_name})


async def _own_note(ctx: TenantContext, conversation_id: uuid.UUID, note_id: uuid.UUID) -> ConversationNote:
    note = await inbox.get_note(ctx.session, ctx.tenant_id, note_id)
    if note.conversation_id != conversation_id:
        raise NotFound("Note not found.", code="note_not_found")
    return note


@router.patch("/conversations/{conversation_id}/notes/{note_id}", response_model=NoteOut)
async def edit_note(
    conversation_id: uuid.UUID, note_id: uuid.UUID, body: NoteIn, ctx: Replier, meta: Meta
) -> NoteOut:
    """Only the author can edit a note."""
    note = await _own_note(ctx, conversation_id, note_id)
    if note.author_user_id != ctx.user.id:
        raise Forbidden("Only the person who wrote a note can edit it.", code="not_note_author")
    text = body.body.strip()
    if not text:
        raise ValidationFailed("The note is empty.", code="empty_note")
    note.body = text
    await record(
        ctx.session,
        "conversation.note_edited",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="conversation_note",
        target_id=note.id,
        details={"conversation_id": str(conversation_id)},
    )
    await ctx.session.flush()
    await ctx.session.refresh(note)
    return _note_out(ctx, note, {ctx.user.id: ctx.user.full_name})


@router.delete("/conversations/{conversation_id}/notes/{note_id}", status_code=204)
async def delete_note(conversation_id: uuid.UUID, note_id: uuid.UUID, ctx: Replier, meta: Meta) -> Response:
    """The author, or a supervisor or above, can delete a note."""
    note = await _own_note(ctx, conversation_id, note_id)
    if note.author_user_id != ctx.user.id and not ctx.can(Permission.CONVERSATIONS_ASSIGN):
        raise Forbidden("Only the author or a supervisor can delete this note.", code="not_note_author")
    await ctx.session.delete(note)
    await record(
        ctx.session,
        "conversation.note_deleted",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="conversation",
        target_id=conversation_id,
        details={"note_id": str(note_id)},
    )
    return Response(status_code=204)


# ---- summary --------------------------------------------------------------------------


class SummaryOut(BaseModel):
    status: str
    summary: str | None
    error: str | None
    provider: str | None
    model: str | None
    generated_at: datetime | None
    message_count: int
    # Messages that arrived after the summary was written.
    new_messages: int


async def _summary_out(ctx: TenantContext, row: ConversationSummary) -> SummaryOut:
    total = int(
        await ctx.session.scalar(
            select(func.count()).select_from(Message).where(Message.conversation_id == row.conversation_id)
        )
        or 0
    )
    return SummaryOut(
        status=row.status,
        summary=row.summary,
        error=row.error,
        provider=row.provider,
        model=row.model,
        generated_at=row.generated_at,
        message_count=row.message_count,
        new_messages=max(0, total - row.message_count) if row.status == SummaryStatus.READY else 0,
    )


@router.get("/conversations/{conversation_id}/summary", response_model=SummaryOut | None)
async def get_summary(conversation_id: uuid.UUID, ctx: Reader) -> SummaryOut | None:
    conv = await inbox.get_conversation(ctx.session, ctx.tenant_id, conversation_id)
    row = (
        await ctx.session.execute(
            select(ConversationSummary).where(ConversationSummary.conversation_id == conv.id)
        )
    ).scalar_one_or_none()
    return await _summary_out(ctx, row) if row else None


@router.post("/conversations/{conversation_id}/summary", response_model=SummaryOut, status_code=202)
async def request_summary(conversation_id: uuid.UUID, ctx: Replier) -> SummaryOut:
    """Ask the assistant's models for a fresh summary. It is written in the background;
    poll GET until the status is ready or failed. Usage counts as purpose 'summary'."""
    conv = await inbox.get_conversation(ctx.session, ctx.tenant_id, conversation_id)
    existing = (
        await ctx.session.execute(
            select(ConversationSummary.status, ConversationSummary.updated_at).where(
                ConversationSummary.conversation_id == conv.id
            )
        )
    ).one_or_none()
    recently_queued = (
        existing is not None
        and existing.status == SummaryStatus.QUEUED
        and existing.updated_at > datetime.now(UTC) - SUMMARY_REQUEUE_AFTER
    )
    row = await inbox.request_summary(ctx.session, conv, ctx.user.id)
    if not recently_queued:
        tenant_id = ctx.tenant_id
        run_after_commit(ctx.session, lambda: dispatch(CONVERSATION_SUMMARY, conv.id, tenant_id))
    return await _summary_out(ctx, row)
