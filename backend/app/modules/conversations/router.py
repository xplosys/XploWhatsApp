"""Conversation timeline and sending. The full inbox (assignment, notes, search,
handover) arrives in Phase 5; this is the part WhatsApp messaging needs now."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from app.api.deps import Meta, Requires, TenantContext
from app.api.pagination import Page, PageParams, page_params
from app.core.errors import NotFound
from app.db.session import run_after_commit
from app.modules.audit.service import record
from app.modules.conversations import outbound
from app.modules.conversations.models import ConsentStatus, Contact, Conversation, Message, SenderType
from app.modules.conversations.service import service_window_open_at, set_consent
from app.modules.rbac.permissions import Permission
from app.modules.whatsapp.models import WhatsAppNumber

router = APIRouter(tags=["conversations"])
Pagination = Annotated[PageParams, Depends(page_params)]
Reader = Annotated[TenantContext, Requires(Permission.CONVERSATIONS_READ)]
Replier = Annotated[TenantContext, Requires(Permission.CONVERSATIONS_REPLY)]
ContactManager = Annotated[TenantContext, Requires(Permission.CONTACTS_MANAGE)]


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    wa_id: str | None
    phone_e164: str | None
    bsuid: str | None
    username: str | None
    profile_name: str | None
    name: str | None
    consent_status: str
    consent_updated_at: datetime | None
    last_inbound_at: datetime | None


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


def _conversation_out(
    c: Conversation, contact: Contact, display: str | None, preview: str | None, now: datetime
) -> ConversationOut:
    return ConversationOut(
        id=c.id,
        contact=ContactOut.model_validate(contact),
        number_id=c.number_id,
        number_display=display,
        status=c.status,
        unread_count=c.unread_count,
        last_inbound_at=c.last_inbound_at,
        last_message_at=c.last_message_at,
        service_window_expires_at=c.service_window_expires_at,
        service_window_open=service_window_open_at(c, now),
        last_message_preview=(preview or "")[:120] or None,
        ai_enabled=c.ai_enabled,
        needs_human=c.needs_human,
    )


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


@router.get("/conversations", response_model=Page[ConversationOut])
async def list_conversations(
    ctx: Reader,
    page: Pagination,
    number_id: uuid.UUID | None = None,
    status: Annotated[Literal["open", "closed"] | None, Query()] = None,
) -> Page[ConversationOut]:
    base = select(Conversation).where(Conversation.tenant_id == ctx.tenant_id)
    if number_id:
        base = base.where(Conversation.number_id == number_id)
    if status:
        base = base.where(Conversation.status == status)
    total = int(await ctx.session.scalar(select(func.count()).select_from(base.subquery())) or 0)
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
            select(Conversation, Contact, WhatsAppNumber.display_phone_number, last_body)
            .join(Contact, Contact.id == Conversation.contact_id)
            .join(WhatsAppNumber, WhatsAppNumber.id == Conversation.number_id)
            .where(Conversation.id.in_(base.with_only_columns(Conversation.id)))
            .order_by(Conversation.last_message_at.desc().nulls_last())
            .limit(page.limit)
            .offset(page.offset)
        )
    ).all()
    now = datetime.now(UTC)
    items = [_conversation_out(c, contact, display, preview, now) for c, contact, display, preview in rows]
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


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
    return await _single_out(ctx, conv)


async def _single_out(ctx: TenantContext, conv: Conversation) -> ConversationOut:
    contact = await ctx.session.get(Contact, conv.contact_id)
    number = await ctx.session.get(WhatsAppNumber, conv.number_id)
    assert contact is not None and number is not None
    return _conversation_out(conv, contact, number.display_phone_number, None, datetime.now(UTC))


@router.put("/conversations/{conversation_id}/ai", response_model=ConversationOut)
async def set_conversation_ai(
    conversation_id: uuid.UUID, body: ConversationAIUpdate, ctx: Replier, meta: Meta
) -> ConversationOut:
    """Turn automatic replies on or off for one conversation. Turning them back on
    also clears the 'needs a person' flag set by a handover."""
    conv = await _get_conversation(ctx, conversation_id)
    conv.ai_enabled = body.enabled
    if body.enabled:
        conv.needs_human = False
    await record(
        ctx.session,
        "conversation.ai_enabled" if body.enabled else "conversation.ai_disabled",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="conversation",
        target_id=conv.id,
    )
    return await _single_out(ctx, conv)


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
