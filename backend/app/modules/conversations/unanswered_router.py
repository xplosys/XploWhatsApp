"""Questions the assistant could not answer, so the business can fill the gaps in its
knowledge base or assistant settings. Listed on the Knowledge page."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import ColumnElement, func, select

from app.api.deps import Meta, Requires, TenantContext
from app.api.pagination import Page, PageParams, page_params
from app.core.errors import NotFound
from app.modules.audit.service import record
from app.modules.conversations.models import Contact, Conversation, UnansweredQuestion, UnansweredStatus
from app.modules.conversations.schemas import UserRef, user_names, user_ref
from app.modules.rbac.permissions import Permission

router = APIRouter(prefix="/knowledge/unanswered", tags=["knowledge"])
Pagination = Annotated[PageParams, Depends(page_params)]
Reader = Annotated[TenantContext, Requires(Permission.KNOWLEDGE_READ)]
Manager = Annotated[TenantContext, Requires(Permission.KNOWLEDGE_MANAGE)]


class UnansweredOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    contact_name: str | None
    question: str
    reason: str
    retrieval_hits: int | None
    status: str
    resolved_by: UserRef | None
    resolved_at: datetime | None
    created_at: datetime


class UnansweredPage(Page[UnansweredOut]):
    open: int


class UnansweredUpdate(BaseModel):
    status: Literal["open", "resolved", "dismissed"]


def _out(row: UnansweredQuestion, contact_name: str | None, names: dict[uuid.UUID, str]) -> UnansweredOut:
    return UnansweredOut(
        id=row.id,
        conversation_id=row.conversation_id,
        message_id=row.message_id,
        contact_name=contact_name,
        question=row.question,
        reason=row.reason,
        retrieval_hits=row.retrieval_hits,
        status=row.status,
        resolved_by=user_ref(row.resolved_by_user_id, names),
        resolved_at=row.resolved_at,
        created_at=row.created_at,
    )


def _contact_name() -> ColumnElement[str | None]:
    return func.coalesce(Contact.name, Contact.profile_name, Contact.phone_e164, Contact.username)


@router.get("", response_model=UnansweredPage)
async def list_unanswered(
    ctx: Reader, page: Pagination, status: Literal["open", "resolved", "dismissed", "all"] = "open"
) -> UnansweredPage:
    """Newest first. `reason` says why the assistant handed over: it asked for a person
    (needs_human), every model failed (all_failed), or its reply was blocked."""
    base = select(UnansweredQuestion).where(UnansweredQuestion.tenant_id == ctx.tenant_id)
    if status != "all":
        base = base.where(UnansweredQuestion.status == status)
    total = int(await ctx.session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    open_count = int(
        await ctx.session.scalar(
            select(func.count()).where(
                UnansweredQuestion.tenant_id == ctx.tenant_id,
                UnansweredQuestion.status == UnansweredStatus.OPEN,
            )
        )
        or 0
    )
    rows = (
        await ctx.session.execute(
            select(UnansweredQuestion, _contact_name())
            .join(Conversation, Conversation.id == UnansweredQuestion.conversation_id)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(UnansweredQuestion.id.in_(base.with_only_columns(UnansweredQuestion.id)))
            .order_by(UnansweredQuestion.created_at.desc(), UnansweredQuestion.id)
            .limit(page.limit)
            .offset(page.offset)
        )
    ).all()
    names = await user_names(ctx.session, [r.resolved_by_user_id for r, _ in rows])
    return UnansweredPage(
        items=[_out(r, name, names) for r, name in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
        open=open_count,
    )


@router.patch("/{question_id}", response_model=UnansweredOut)
async def update_unanswered(
    question_id: uuid.UUID, body: UnansweredUpdate, ctx: Manager, meta: Meta
) -> UnansweredOut:
    """Mark a question resolved (the answer is now in the knowledge base or settings),
    dismissed (nothing to add), or open again."""
    row = await ctx.session.get(UnansweredQuestion, question_id)
    if row is None or row.tenant_id != ctx.tenant_id:
        raise NotFound("Question not found.", code="unanswered_not_found")
    row.status = body.status
    if body.status == "open":
        row.resolved_by_user_id, row.resolved_at = None, None
    else:
        row.resolved_by_user_id, row.resolved_at = ctx.user.id, datetime.now(UTC)
    await record(
        ctx.session,
        "unanswered.updated",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="unanswered_question",
        target_id=row.id,
        details={"status": body.status},
    )
    await ctx.session.flush()
    await ctx.session.refresh(row)
    name = await ctx.session.scalar(
        select(_contact_name())
        .select_from(Conversation)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.id == row.conversation_id)
    )
    return _out(row, name, {ctx.user.id: ctx.user.full_name})
