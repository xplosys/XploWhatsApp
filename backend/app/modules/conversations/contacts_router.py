"""Contacts: the people a business talks to on WhatsApp, their details and tags."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import Meta, Requires, TenantContext
from app.api.pagination import Page, PageParams, page_params
from app.modules.audit.service import record
from app.modules.conversations import inbox
from app.modules.conversations.models import Contact, Conversation
from app.modules.conversations.router import contact_matches, like_pattern
from app.modules.conversations.schemas import ContactOut, UserRef, user_names, user_ref
from app.modules.leads.models import Lead
from app.modules.rbac.permissions import Permission
from app.modules.whatsapp.models import WhatsAppNumber

router = APIRouter(tags=["contacts"])
Pagination = Annotated[PageParams, Depends(page_params)]
Reader = Annotated[TenantContext, Requires(Permission.CONVERSATIONS_READ)]
ContactManager = Annotated[TenantContext, Requires(Permission.CONTACTS_MANAGE)]


class ContactConversation(BaseModel):
    id: uuid.UUID
    number_id: uuid.UUID
    number_display: str | None
    status: str
    last_message_at: datetime | None
    assigned_to: UserRef | None
    ai_enabled: bool
    needs_human: bool


class ContactLead(BaseModel):
    id: uuid.UUID
    title: str
    status: str
    value: Decimal | None
    currency: str | None
    owner: UserRef | None
    updated_at: datetime


class ContactDetail(ContactOut):
    conversations: list[ContactConversation]
    leads: list[ContactLead]


class ContactUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    language: str | None = Field(
        default=None, max_length=16, pattern=r"^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})*$"
    )
    tags: list[str] | None = Field(default=None, max_length=50)


class TagCount(BaseModel):
    tag: str
    contacts: int


@router.get("/contacts", response_model=Page[ContactOut])
async def list_contacts(
    ctx: Reader,
    page: Pagination,
    q: Annotated[str | None, Query(min_length=2, max_length=100)] = None,
    tag: Annotated[str | None, Query(max_length=40)] = None,
    consent: Literal["unknown", "opted_in", "opted_out"] | None = None,
) -> Page[ContactOut]:
    """Contacts, most recently active first. `q` matches name, WhatsApp name, phone or username."""
    base = select(Contact).where(Contact.tenant_id == ctx.tenant_id)
    if q and q.strip():
        base = base.where(contact_matches(like_pattern(q)))
    if tag:
        base = base.where(Contact.tags.contains([tag.strip().lower()]))
    if consent:
        base = base.where(Contact.consent_status == consent)
    total = int(await ctx.session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = (
        await ctx.session.execute(
            base.order_by(Contact.last_inbound_at.desc().nulls_last(), Contact.created_at.desc(), Contact.id)
            .limit(page.limit)
            .offset(page.offset)
        )
    ).scalars()
    return Page(
        items=[ContactOut.model_validate(c) for c in rows], total=total, limit=page.limit, offset=page.offset
    )


@router.get("/contacts/tags", response_model=list[TagCount])
async def list_tags(ctx: Reader) -> list[TagCount]:
    """Every tag in use, with how many contacts carry it, for filters and suggestions."""
    tag = func.unnest(Contact.tags).label("tag")
    sub = select(tag).where(Contact.tenant_id == ctx.tenant_id).subquery()
    rows = await ctx.session.execute(
        select(sub.c.tag, func.count())
        .group_by(sub.c.tag)
        .order_by(func.count().desc(), sub.c.tag)
        .limit(200)
    )
    return [TagCount(tag=t, contacts=n) for t, n in rows.all()]


async def _detail(ctx: TenantContext, contact: Contact) -> ContactDetail:
    convs = (
        await ctx.session.execute(
            select(Conversation, WhatsAppNumber.display_phone_number)
            .join(WhatsAppNumber, WhatsAppNumber.id == Conversation.number_id)
            .where(Conversation.contact_id == contact.id)
            .order_by(Conversation.last_message_at.desc().nulls_last())
        )
    ).all()
    leads = list(
        (
            await ctx.session.execute(
                select(Lead).where(Lead.contact_id == contact.id).order_by(Lead.updated_at.desc()).limit(50)
            )
        ).scalars()
    )
    names = await user_names(
        ctx.session, [c.assigned_user_id for c, _ in convs] + [lead.owner_user_id for lead in leads]
    )
    base = ContactOut.model_validate(contact).model_dump()
    return ContactDetail(
        **base,
        conversations=[
            ContactConversation(
                id=c.id,
                number_id=c.number_id,
                number_display=display,
                status=c.status,
                last_message_at=c.last_message_at,
                assigned_to=user_ref(c.assigned_user_id, names),
                ai_enabled=c.ai_enabled,
                needs_human=c.needs_human,
            )
            for c, display in convs
        ],
        leads=[
            ContactLead(
                id=lead.id,
                title=lead.title,
                status=lead.status,
                value=lead.value,
                currency=lead.currency,
                owner=user_ref(lead.owner_user_id, names),
                updated_at=lead.updated_at,
            )
            for lead in leads
        ],
    )


@router.get("/contacts/{contact_id}", response_model=ContactDetail)
async def get_contact(contact_id: uuid.UUID, ctx: Reader) -> ContactDetail:
    """A contact with their conversations (one per business number) and leads."""
    return await _detail(ctx, await inbox.get_contact(ctx.session, ctx.tenant_id, contact_id))


@router.patch("/contacts/{contact_id}", response_model=ContactDetail)
async def update_contact(
    contact_id: uuid.UUID, body: ContactUpdate, ctx: ContactManager, meta: Meta
) -> ContactDetail:
    """Change the name the business uses for a contact, their language, or their tags.
    The WhatsApp profile name and number come from WhatsApp and are not editable."""
    contact = await inbox.get_contact(ctx.session, ctx.tenant_id, contact_id)
    changes: dict[str, object] = {}
    fields = body.model_fields_set
    if "name" in fields:
        name = (body.name or "").strip() or None
        if name != contact.name:
            contact.name = name
            changes["name"] = name
    if "language" in fields:
        language = (body.language or "").strip().replace("_", "-") or None
        if language != contact.language:
            contact.language = language
            changes["language"] = language
    if "tags" in fields:
        tags = inbox.clean_tags(body.tags or [])
        if tags != list(contact.tags or []):
            contact.tags = tags
            changes["tags"] = tags
    if changes:
        await record(
            ctx.session,
            "contact.updated",
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="contact",
            target_id=contact.id,
            details=changes,
        )
        await ctx.session.flush()
        await ctx.session.refresh(contact)
    return await _detail(ctx, contact)
