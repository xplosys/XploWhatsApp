"""Leads: sales opportunities with a contact, usually started from a WhatsApp conversation."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select

from app.api.deps import Meta, Requires, TenantContext
from app.api.pagination import Page, PageParams, page_params
from app.core.errors import ValidationFailed
from app.modules.audit.service import record
from app.modules.conversations import inbox
from app.modules.conversations.models import Contact
from app.modules.conversations.router import contact_matches, like_pattern
from app.modules.conversations.schemas import UserRef, user_names, user_ref
from app.modules.leads import service
from app.modules.leads.models import CLOSED_STATUSES, Lead, LeadActivity, LeadStatus
from app.modules.rbac.permissions import Permission

router = APIRouter(prefix="/leads", tags=["leads"])
Pagination = Annotated[PageParams, Depends(page_params)]
LeadManager = Annotated[TenantContext, Requires(Permission.LEADS_MANAGE)]
Supervisor = Annotated[TenantContext, Requires(Permission.CONVERSATIONS_ASSIGN)]

StatusName = Literal["new", "contacted", "qualified", "won", "lost"]
Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


class LeadContact(BaseModel):
    id: uuid.UUID
    name: str | None
    phone_e164: str | None


class LeadOut(BaseModel):
    id: uuid.UUID
    title: str
    status: str
    source: str
    value: Decimal | None
    currency: str | None
    contact: LeadContact
    conversation_id: uuid.UUID | None
    owner: UserRef | None
    created_by: UserRef | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


class ActivityOut(BaseModel):
    id: uuid.UUID
    type: str
    data: dict[str, Any]
    actor: UserRef | None
    created_at: datetime


class LeadDetail(LeadOut):
    activities: list[ActivityOut]


class LeadCreate(BaseModel):
    contact_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    status: StatusName = "new"
    value: Money | None = None
    currency: str | None = Field(default=None, max_length=3)
    owner_user_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _who(self) -> LeadCreate:
        if not self.contact_id and not self.conversation_id:
            raise ValueError("Give contact_id or conversation_id.")
        return self


class LeadUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: StatusName | None = None
    value: Money | None = None
    currency: str | None = Field(default=None, max_length=3)
    owner_user_id: uuid.UUID | None = None


class LeadNote(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class StatusCount(BaseModel):
    status: str
    count: int


class OpenValue(BaseModel):
    currency: str
    total: Decimal


class LeadSummary(BaseModel):
    by_status: list[StatusCount]
    open_value: list[OpenValue]
    mine_open: int
    unowned_open: int
    default_currency: str | None


def _display_name(contact: Contact) -> str | None:
    return contact.name or contact.profile_name or contact.username


async def _rows_out(ctx: TenantContext, rows: list[tuple[Lead, Contact]]) -> list[LeadOut]:
    names = await user_names(
        ctx.session, [lead.owner_user_id for lead, _ in rows] + [lead.created_by_user_id for lead, _ in rows]
    )
    return [
        LeadOut(
            id=lead.id,
            title=lead.title,
            status=lead.status,
            source=lead.source,
            value=lead.value,
            currency=lead.currency,
            contact=LeadContact(id=contact.id, name=_display_name(contact), phone_e164=contact.phone_e164),
            conversation_id=lead.conversation_id,
            owner=user_ref(lead.owner_user_id, names),
            created_by=user_ref(lead.created_by_user_id, names),
            created_at=lead.created_at,
            updated_at=lead.updated_at,
            closed_at=lead.closed_at,
        )
        for lead, contact in rows
    ]


async def _detail(ctx: TenantContext, lead: Lead) -> LeadDetail:
    await ctx.session.flush()
    await ctx.session.refresh(lead)
    contact = await inbox.get_contact(ctx.session, ctx.tenant_id, lead.contact_id)
    [base] = await _rows_out(ctx, [(lead, contact)])
    activities = list(
        (
            await ctx.session.execute(
                select(LeadActivity)
                .where(LeadActivity.lead_id == lead.id)
                .order_by(LeadActivity.created_at.desc(), LeadActivity.id)
                .limit(200)
            )
        ).scalars()
    )
    names = await user_names(ctx.session, [a.actor_user_id for a in activities])
    return LeadDetail(
        **base.model_dump(),
        activities=[
            ActivityOut(
                id=a.id,
                type=a.type,
                data=a.data,
                actor=user_ref(a.actor_user_id, names),
                created_at=a.created_at,
            )
            for a in activities
        ],
    )


@router.get("", response_model=Page[LeadOut])
async def list_leads(
    ctx: LeadManager,
    page: Pagination,
    status: Literal["new", "contacted", "qualified", "won", "lost", "open", "closed"] | None = None,
    owner: Annotated[str | None, Query(description="'me', 'none', or a member's user id")] = None,
    contact_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(min_length=2, max_length=100)] = None,
) -> Page[LeadOut]:
    """Leads, most recently changed first. `status=open` means not yet won or lost."""
    base = select(Lead.id).join(Contact, Contact.id == Lead.contact_id).where(Lead.tenant_id == ctx.tenant_id)
    closed = [s.value for s in CLOSED_STATUSES]
    if status == "open":
        base = base.where(Lead.status.not_in(closed))
    elif status == "closed":
        base = base.where(Lead.status.in_(closed))
    elif status:
        base = base.where(Lead.status == status)
    if owner == "me":
        base = base.where(Lead.owner_user_id == ctx.user.id)
    elif owner == "none":
        base = base.where(Lead.owner_user_id.is_(None))
    elif owner:
        try:
            base = base.where(Lead.owner_user_id == uuid.UUID(owner))
        except ValueError:
            raise ValidationFailed("owner must be 'me', 'none' or a user id.", code="invalid_owner") from None
    if contact_id:
        base = base.where(Lead.contact_id == contact_id)
    if q and q.strip():
        pattern = like_pattern(q)
        base = base.where(func.lower(Lead.title).like(pattern, escape="\\") | contact_matches(pattern))
    total = int(await ctx.session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = (
        await ctx.session.execute(
            select(Lead, Contact)
            .join(Contact, Contact.id == Lead.contact_id)
            .where(Lead.id.in_(base))
            .order_by(Lead.updated_at.desc(), Lead.id)
            .limit(page.limit)
            .offset(page.offset)
        )
    ).all()
    items = await _rows_out(ctx, [(lead, contact) for lead, contact in rows])
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


@router.get("/summary", response_model=LeadSummary)
async def lead_summary(ctx: LeadManager) -> LeadSummary:
    """Counts by status and the value of open leads per currency, for the pipeline header."""
    closed = [s.value for s in CLOSED_STATUSES]
    counts = dict(
        (
            await ctx.session.execute(
                select(Lead.status, func.count()).where(Lead.tenant_id == ctx.tenant_id).group_by(Lead.status)
            )
        ).all()
    )
    values = (
        await ctx.session.execute(
            select(Lead.currency, func.sum(Lead.value))
            .where(
                Lead.tenant_id == ctx.tenant_id,
                Lead.status.not_in(closed),
                Lead.value.is_not(None),
                Lead.currency.is_not(None),
            )
            .group_by(Lead.currency)
            .order_by(Lead.currency)
        )
    ).all()
    open_filter = (Lead.tenant_id == ctx.tenant_id, Lead.status.not_in(closed))
    mine = int(
        await ctx.session.scalar(select(func.count()).where(*open_filter, Lead.owner_user_id == ctx.user.id))
        or 0
    )
    unowned = int(
        await ctx.session.scalar(select(func.count()).where(*open_filter, Lead.owner_user_id.is_(None))) or 0
    )
    return LeadSummary(
        by_status=[StatusCount(status=s.value, count=int(counts.get(s.value, 0))) for s in LeadStatus],
        open_value=[OpenValue(currency=c, total=t) for c, t in values],
        mine_open=mine,
        unowned_open=unowned,
        default_currency=service.default_currency(ctx.tenant.country),
    )


@router.post("", response_model=LeadDetail, status_code=201)
async def create_lead(body: LeadCreate, ctx: LeadManager, meta: Meta) -> LeadDetail:
    """Create a lead for a contact, or from a conversation (its contact, source whatsapp)."""
    conversation_id = None
    if body.conversation_id:
        conv = await inbox.get_conversation(ctx.session, ctx.tenant_id, body.conversation_id)
        if body.contact_id and body.contact_id != conv.contact_id:
            raise ValidationFailed("That conversation belongs to another contact.", code="contact_mismatch")
        contact_id, conversation_id = conv.contact_id, conv.id
    else:
        assert body.contact_id is not None
        contact_id = (await inbox.get_contact(ctx.session, ctx.tenant_id, body.contact_id)).id
    await service.check_owner(
        ctx.session,
        ctx.tenant_id,
        actor_id=ctx.user.id,
        can_assign_others=ctx.can(Permission.CONVERSATIONS_ASSIGN),
        current=None,
        new=body.owner_user_id,
    )
    currency = service.clean_currency(body.currency)
    if body.value is not None and currency is None:
        currency = service.default_currency(ctx.tenant.country)
        if currency is None:
            raise ValidationFailed("Give the currency for this value.", code="currency_required")
    lead = Lead(
        tenant_id=ctx.tenant_id,
        contact_id=contact_id,
        conversation_id=conversation_id,
        title=body.title.strip(),
        status=body.status,
        source="whatsapp" if conversation_id else "manual",
        value=body.value,
        currency=currency,
        owner_user_id=body.owner_user_id,
        created_by_user_id=ctx.user.id,
    )
    if body.status in CLOSED_STATUSES:
        lead.closed_at = func.now()
    ctx.session.add(lead)
    await ctx.session.flush()
    service.log_activity(
        ctx.session, lead, "created", ctx.user.id, {"status": lead.status, "source": lead.source}
    )
    if body.note and body.note.strip():
        service.log_activity(ctx.session, lead, "note", ctx.user.id, {"body": body.note.strip()})
    await record(
        ctx.session,
        "lead.created",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="lead",
        target_id=lead.id,
    )
    return await _detail(ctx, lead)


@router.get("/{lead_id}", response_model=LeadDetail)
async def get_lead(lead_id: uuid.UUID, ctx: LeadManager) -> LeadDetail:
    return await _detail(ctx, await service.get_lead(ctx.session, ctx.tenant_id, lead_id))


@router.patch("/{lead_id}", response_model=LeadDetail)
async def update_lead(lead_id: uuid.UUID, body: LeadUpdate, ctx: LeadManager) -> LeadDetail:
    """Change any of title, status, value and currency, or owner. Each change is
    written to the lead's history."""
    lead = await service.get_lead(ctx.session, ctx.tenant_id, lead_id, lock=True)
    fields = body.model_fields_set
    if "title" in fields and body.title is not None and body.title.strip() != lead.title:
        service.log_activity(
            ctx.session, lead, "title_changed", ctx.user.id, {"from": lead.title, "to": body.title.strip()}
        )
        lead.title = body.title.strip()
    if "status" in fields and body.status is not None:
        service.set_status(ctx.session, lead, body.status, ctx.user.id)
    if fields & {"value", "currency"}:
        value = body.value if "value" in fields else lead.value
        currency = service.clean_currency(body.currency) if "currency" in fields else lead.currency
        if value is not None and currency is None:
            currency = service.default_currency(ctx.tenant.country)
            if currency is None:
                raise ValidationFailed("Give the currency for this value.", code="currency_required")
        service.set_value(ctx.session, lead, value, currency, ctx.user.id)
    if "owner_user_id" in fields and body.owner_user_id != lead.owner_user_id:
        await service.check_owner(
            ctx.session,
            ctx.tenant_id,
            actor_id=ctx.user.id,
            can_assign_others=ctx.can(Permission.CONVERSATIONS_ASSIGN),
            current=lead.owner_user_id,
            new=body.owner_user_id,
        )
        service.log_activity(
            ctx.session,
            lead,
            "owner_changed",
            ctx.user.id,
            {
                "from": str(lead.owner_user_id) if lead.owner_user_id else None,
                "to": str(body.owner_user_id) if body.owner_user_id else None,
            },
        )
        lead.owner_user_id = body.owner_user_id
    return await _detail(ctx, lead)


@router.post("/{lead_id}/notes", response_model=LeadDetail, status_code=201)
async def add_lead_note(lead_id: uuid.UUID, body: LeadNote, ctx: LeadManager) -> LeadDetail:
    lead = await service.get_lead(ctx.session, ctx.tenant_id, lead_id, lock=True)
    text = body.body.strip()
    if not text:
        raise ValidationFailed("The note is empty.", code="empty_note")
    service.log_activity(ctx.session, lead, "note", ctx.user.id, {"body": text})
    lead.updated_at = func.now()
    return await _detail(ctx, lead)


@router.delete("/{lead_id}", status_code=204)
async def delete_lead(lead_id: uuid.UUID, ctx: Supervisor, meta: Meta) -> Response:
    """Delete a lead and its history (supervisors and above). Prefer marking it lost."""
    lead = await service.get_lead(ctx.session, ctx.tenant_id, lead_id, lock=True)
    await record(
        ctx.session,
        "lead.deleted",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="lead",
        target_id=lead.id,
        details={"title": lead.title, "status": lead.status},
    )
    await ctx.session.delete(lead)
    return Response(status_code=204)
