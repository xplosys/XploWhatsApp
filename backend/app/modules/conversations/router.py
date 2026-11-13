"""The inbox list and search, the conversation timeline, and sending. The agent
workflow on one conversation (assignment, takeover, notes, summary) is in
`inbox_router`; contacts are in `contacts_router`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import ColumnElement, Select, func, or_, select

from app.api.deps import Meta, Requires, TenantContext
from app.api.pagination import Page, PageParams, page_params
from app.core.errors import NotFound
from app.db.session import run_after_commit
from app.modules.audit.service import record
from app.modules.conversations import inbox, outbound
from app.modules.conversations.models import (
    ConsentStatus,
    Contact,
    Conversation,
    HandoverStarter,
    Message,
    SenderType,
)
from app.modules.conversations.schemas import ContactOut, ConversationOut, conversations_out
from app.modules.conversations.service import set_consent
from app.modules.rbac.permissions import Permission
from app.modules.whatsapp.models import WhatsAppNumber

router = APIRouter(tags=["conversations"])
Pagination = Annotated[PageParams, Depends(page_params)]
Reader = Annotated[TenantContext, Requires(Permission.CONVERSATIONS_READ)]
Replier = Annotated[TenantContext, Requires(Permission.CONVERSATIONS_REPLY)]
ContactManager = Annotated[TenantContext, Requires(Permission.CONTACTS_MANAGE)]


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    direction: str
    sender_type: str
    sent_by_user_id: uuid.UUID | None
    type: str
    body: str | None
    payload: dict[str, Any]
    status: str
    error_code: str | None
    error_message: str | None
    pricing_category: str | None
    wa_timestamp: datetime | None
    created_at: datetime
    status_updated_at: datetime | None
    ai_provenance: dict[str, Any] | None


class SendMessage(BaseModel):
    number_id: uuid.UUID
    contact_id: uuid.UUID | None = None
    phone: str | None = Field(default=None, max_length=32)
    text: str | None = Field(default=None, max_length=outbound.MAX_TEXT_LENGTH)
    template_name: str | None = Field(default=None, max_length=512)
    template_language: str | None = Field(default=None, max_length=16)
    template_components: list[dict[str, Any]] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def _one_recipient_one_content(self) -> SendMessage:
        if bool(self.contact_id) == bool(self.phone):
            raise ValueError("Give either contact_id or phone.")
        if bool(self.text) == bool(self.template_name):
            raise ValueError("Give either text or template_name.")
        return self


class ConversationReply(BaseModel):
    text: str | None = Field(default=None, max_length=outbound.MAX_TEXT_LENGTH)
    template_name: str | None = Field(default=None, max_length=512)
    template_language: str | None = Field(default=None, max_length=16)
    template_components: list[dict[str, Any]] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def _one_content(self) -> ConversationReply:
        if bool(self.text) == bool(self.template_name):
            raise ValueError("Give either text or template_name.")
        return self


class ConversationAIUpdate(BaseModel):
    enabled: bool


class ConsentUpdate(BaseModel):
    status: Literal["opted_in", "opted_out"]
    source: str = Field(
        min_length=2, max_length=64, description="Where consent was collected, e.g. 'website form'."
    )


async def _queue(ctx: TenantContext, req: outbound.OutboundRequest) -> MessageOut:
    msg = await outbound.queue_message(ctx.session, ctx.tenant_id, req)
    job = await outbound.outbox_job_for(ctx.session, msg.id)
    assert job is not None
    run_after_commit(ctx.session, lambda: outbound.dispatch_job(job))
    return MessageOut.model_validate(msg)


@router.post("/messages", response_model=MessageOut, status_code=202)
async def send_message(body: SendMessage, ctx: Replier) -> MessageOut:
    """Queue a message. Text is allowed inside the 24-hour customer-service window;
    outside it, or to start a conversation, use an approved template."""
    return await _queue(
        ctx,
        outbound.OutboundRequest(
            number_id=body.number_id,
            contact_id=body.contact_id,
            phone=body.phone,
            text=body.text,
            template_name=body.template_name,
            template_language=body.template_language,
            template_components=body.template_components,
            sender_type=SenderType.AGENT,
            sent_by_user_id=ctx.user.id,
        ),
    )


@router.post("/conversations/{conversation_id}/messages", response_model=MessageOut, status_code=202)
async def reply(conversation_id: uuid.UUID, body: ConversationReply, ctx: Replier) -> MessageOut:
    conv = await _get_conversation(ctx, conversation_id)
    return await _queue(
        ctx,
        outbound.OutboundRequest(
            number_id=conv.number_id,
            contact_id=conv.contact_id,
            text=body.text,
            template_name=body.template_name,
            template_language=body.template_language,
            template_components=body.template_components,
            sender_type=SenderType.AGENT,
            sent_by_user_id=ctx.user.id,
        ),
    )


async def _get_conversation(ctx: TenantContext, conversation_id: uuid.UUID) -> Conversation:
    conv = await ctx.session.get(Conversation, conversation_id)
    if conv is None or conv.tenant_id != ctx.tenant_id:
        raise NotFound("Conversation not found.")
    return conv


InboxView = Literal["all", "open", "mine", "unassigned", "needs_human", "awaiting", "closed"]


def like_pattern(q: str) -> str:
    """A case-insensitive substring pattern with LIKE wildcards escaped."""
    escaped = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def contact_matches(pattern: str) -> ColumnElement[bool]:
    return or_(
        *(
            func.lower(col).like(pattern, escape="\\")
            for col in (Contact.name, Contact.profile_name, Contact.phone_e164, Contact.username)
        )
    )


def _apply_view(stmt: Select[Any], view: InboxView, user_id: uuid.UUID) -> Select[Any]:
    if view == "all":
        return stmt
    if view == "closed":
        return stmt.where(Conversation.status == "closed")
    stmt = stmt.where(Conversation.status == "open")
    if view == "mine":
        return stmt.where(Conversation.assigned_user_id == user_id)
    if view == "unassigned":
        return stmt.where(Conversation.assigned_user_id.is_(None))
    if view == "needs_human":
        return stmt.where(Conversation.needs_human.is_(True))
    if view == "awaiting":
        return stmt.where(Conversation.awaiting_reply.is_(True))
    return stmt


@router.get("/conversations", response_model=Page[ConversationOut])
async def list_conversations(
    ctx: Reader,
    page: Pagination,
    view: InboxView = "all",
    number_id: uuid.UUID | None = None,
    status: Annotated[Literal["open", "closed"] | None, Query()] = None,
    assigned_to: uuid.UUID | None = None,
    tag: Annotated[str | None, Query(max_length=40)] = None,
    q: Annotated[str | None, Query(min_length=2, max_length=100)] = None,
) -> Page[ConversationOut]:
    """The inbox. `view` picks a tab; `q` matches the contact's name, phone or username,
    or any message text (the preview then shows the newest matching message)."""
    base = (
        select(Conversation.id)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.tenant_id == ctx.tenant_id)
    )
    base = _apply_view(base, view, ctx.user.id)
    if number_id:
        base = base.where(Conversation.number_id == number_id)
    if status:
        base = base.where(Conversation.status == status)
    if assigned_to:
        base = base.where(Conversation.assigned_user_id == assigned_to)
    if tag:
        base = base.where(Contact.tags.contains([tag.strip().lower()]))
    message_filter: ColumnElement[bool] | None = None
    if q and q.strip():
        pattern = like_pattern(q)
        message_filter = func.lower(Message.body).like(pattern, escape="\\")
        body_hit = (
            select(Message.id)
            .where(
                Message.tenant_id == ctx.tenant_id,
                Message.conversation_id == Conversation.id,
                message_filter,
            )
            .exists()
        )
        base = base.where(or_(contact_matches(pattern), body_hit))
    total = int(await ctx.session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    preview = select(Message.body).where(Message.conversation_id == Conversation.id)
    if message_filter is not None:
        preview = preview.where(message_filter)
    preview_body = (
        preview.order_by(Message.created_at.desc()).limit(1).correlate(Conversation).scalar_subquery()
    )
    last_body = (
        select(Message.body)
        .where(Message.conversation_id == Conversation.id)
        .order_by(Message.created_at.desc())
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )
    rows = (
        await ctx.session.execute(
            select(
                Conversation,
                Contact,
                WhatsAppNumber.display_phone_number,
                func.coalesce(preview_body, last_body) if message_filter is not None else last_body,
            )
            .join(Contact, Contact.id == Conversation.contact_id)
            .join(WhatsAppNumber, WhatsAppNumber.id == Conversation.number_id)
            .where(Conversation.id.in_(base))
            .order_by(Conversation.last_message_at.desc().nulls_last(), Conversation.id)
            .limit(page.limit)
            .offset(page.offset)
        )
    ).all()
    items = await conversations_out(ctx.session, [tuple(r) for r in rows], datetime.now(UTC))
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


class InboxCounts(BaseModel):
    open: int
    mine: int
    unassigned: int
    needs_human: int
    awaiting: int
    closed: int
    unread: int


@router.get("/conversations/counts", response_model=InboxCounts)
async def inbox_counts(ctx: Reader, number_id: uuid.UUID | None = None) -> InboxCounts:
    """How many conversations each inbox tab holds."""
    is_open = Conversation.status == "open"
    stmt = select(
        func.count().filter(is_open),
        func.count().filter(is_open, Conversation.assigned_user_id == ctx.user.id),
        func.count().filter(is_open, Conversation.assigned_user_id.is_(None)),
        func.count().filter(is_open, Conversation.needs_human.is_(True)),
        func.count().filter(is_open, Conversation.awaiting_reply.is_(True)),
        func.count().filter(Conversation.status == "closed"),
        func.count().filter(is_open, Conversation.unread_count > 0),
    ).where(Conversation.tenant_id == ctx.tenant_id)
    if number_id:
        stmt = stmt.where(Conversation.number_id == number_id)
    row = (await ctx.session.execute(stmt)).one()
    return InboxCounts(
        open=row[0],
        mine=row[1],
        unassigned=row[2],
        needs_human=row[3],
        awaiting=row[4],
        closed=row[5],
        unread=row[6],
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
async def get_conversation(conversation_id: uuid.UUID, ctx: Reader) -> ConversationOut:
    conv = await _get_conversation(ctx, conversation_id)
    return await single_out(ctx, conv)


@router.get("/conversations/{conversation_id}/messages", response_model=Page[MessageOut])
async def list_messages(conversation_id: uuid.UUID, ctx: Reader, page: Pagination) -> Page[MessageOut]:
    conv = await _get_conversation(ctx, conversation_id)
    base = select(Message).where(Message.tenant_id == ctx.tenant_id, Message.conversation_id == conv.id)
    total = int(await ctx.session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = (
        await ctx.session.execute(
            base.order_by(Message.created_at.desc()).limit(page.limit).offset(page.offset)
        )
    ).scalars()
    items = [MessageOut.model_validate(m) for m in rows]
    return Page(items=list(reversed(items)), total=total, limit=page.limit, offset=page.offset)


@router.post("/conversations/{conversation_id}/read", response_model=ConversationOut)
async def mark_read(conversation_id: uuid.UUID, ctx: Reader) -> ConversationOut:
    conv = await _get_conversation(ctx, conversation_id)
    conv.unread_count = 0
    return await single_out(ctx, conv)


async def single_out(ctx: TenantContext, conv: Conversation) -> ConversationOut:
    await ctx.session.flush()
    contact = await ctx.session.get(Contact, conv.contact_id)
    number = await ctx.session.get(WhatsAppNumber, conv.number_id)
    assert contact is not None and number is not None
    last = await ctx.session.scalar(
        select(Message.body)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    items = await conversations_out(
        ctx.session, [(conv, contact, number.display_phone_number, last)], datetime.now(UTC)
    )
    return items[0]


@router.put("/conversations/{conversation_id}/ai", response_model=ConversationOut)
async def set_conversation_ai(
    conversation_id: uuid.UUID, body: ConversationAIUpdate, ctx: Replier, meta: Meta
) -> ConversationOut:
    """Turn automatic replies on or off for one conversation. Turning them off is a
    takeover by this person; turning them on hands the conversation back to the
    assistant and clears the 'needs a person' flag."""
    conv = await inbox.get_conversation(ctx.session, ctx.tenant_id, conversation_id, lock=True)
    if body.enabled:
        await inbox.release_handover(ctx.session, conv, user_id=ctx.user.id)
    else:
        await inbox.start_handover(
            ctx.session,
            conv,
            started_by=HandoverStarter.AGENT,
            reason="ai_turned_off",
            agent_user_id=ctx.user.id,
        )
    await record(
        ctx.session,
        "conversation.ai_enabled" if body.enabled else "conversation.ai_disabled",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="conversation",
        target_id=conv.id,
    )
    return await single_out(ctx, conv)


@router.put("/contacts/{contact_id}/consent", response_model=ContactOut)
async def update_consent(
    contact_id: uuid.UUID, body: ConsentUpdate, ctx: ContactManager, meta: Meta
) -> ContactOut:
    """Record marketing consent collected outside WhatsApp (or an opt-out request)."""
    contact = await ctx.session.get(Contact, contact_id)
    if contact is None or contact.tenant_id != ctx.tenant_id:
        raise NotFound("Contact not found.")
    set_consent(contact, ConsentStatus(body.status), body.source)
    await record(
        ctx.session,
        "contact.consent_updated",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="contact",
        target_id=contact.id,
        details={"status": body.status, "source": body.source},
    )
    return ContactOut.model_validate(contact)
