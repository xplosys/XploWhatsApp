"""GET /analytics/overview for the Analytics page and the overview tiles, and GET
/analytics/setup for the overview's setup checklist."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import Requires, TenantContext
from app.core.errors import ValidationFailed
from app.modules.ai.models import Assistant
from app.modules.analytics import service
from app.modules.conversations.schemas import user_names
from app.modules.knowledge.models import KnowledgeDocument
from app.modules.rbac.permissions import Permission
from app.modules.tenants.models import TenantMembership
from app.modules.whatsapp.models import NumberStatus, WhatsAppNumber

router = APIRouter(prefix="/analytics", tags=["analytics"])
Reader = Annotated[TenantContext, Requires(Permission.ANALYTICS_READ)]


class TotalsOut(BaseModel):
    conversations_started: int
    messages_in: int
    messages_ai: int
    messages_team: int
    ai_conversations: int
    ai_handovers: int
    ai_resolution_rate: float | None
    leads_created: int
    leads_won: int


class DayOut(BaseModel):
    date: date
    customer: int
    ai: int
    team: int


class HourOut(BaseModel):
    weekday: int
    hour: int
    messages: int


class ReasonOut(BaseModel):
    reason: str
    count: int


class ReplyTime(BaseModel):
    conversations: int
    median_seconds: float | None
    p90_seconds: float | None


class ResponseOut(BaseModel):
    first_reply_ai: ReplyTime | None
    first_reply_team: ReplyTime | None
    first_reply_none: int
    handovers: int
    handovers_answered: int
    handover_median_seconds: float | None


class AgentOut(BaseModel):
    user_id: uuid.UUID
    name: str
    role: str | None
    messages: int
    conversations: int
    handovers: int
    handovers_answered: int
    handover_median_seconds: float | None
    leads_won: int


class MoneyOut(BaseModel):
    currency: str
    total: Decimal


class PurposeOut(BaseModel):
    purpose: str
    calls: int


class CostOut(BaseModel):
    calls: int
    unpriced_calls: int
    cost: list[MoneyOut]
    calls_by_purpose: list[PurposeOut]


class ErrorCount(BaseModel):
    code: str
    count: int


class BillableCount(BaseModel):
    category: str
    messages: int


class DeliveryOut(BaseModel):
    sent: int
    delivered: int
    read: int
    failed: int
    pending: int
    top_errors: list[ErrorCount]
    billable: list[BillableCount]


class Overview(BaseModel):
    days: int
    timezone: str
    start: datetime
    end: datetime
    totals: TotalsOut
    previous: TotalsOut
    daily: list[DayOut]
    hours: list[HourOut]
    handover_reasons: list[ReasonOut]
    response: ResponseOut
    agents: list[AgentOut]
    won_value: list[MoneyOut]
    open_unanswered: int
    delivery: DeliveryOut
    ai_cost: CostOut | None  # only for people who can see AI settings or billing


@router.get("/overview", response_model=Overview)
async def overview(ctx: Reader, days: Annotated[int, Query(ge=1, le=90)] = 30) -> Overview:
    if days not in service.PERIODS:
        raise ValidationFailed("Choose 7, 30 or 90 days.", code="invalid_period")
    s, tid = ctx.session, ctx.tenant_id
    w = service.window(days, ctx.tenant.timezone)
    current = await service.totals(s, tid, w)
    previous = await service.totals(s, tid, w.previous)
    times = await service.response_times(s, tid, w)
    activity = await service.team_activity(s, tid, w)

    roles = dict(
        (
            await s.execute(
                select(TenantMembership.user_id, TenantMembership.role).where(
                    TenantMembership.tenant_id == tid, TenantMembership.status == "active"
                )
            )
        ).all()
    )
    people = set(activity) | set(times.per_agent)
    names = await user_names(s, people)
    agents = []
    for uid in people:
        n, answered, median = times.per_agent.get(uid, (0, 0, None))
        a = activity.get(uid, {})
        agents.append(
            AgentOut(
                user_id=uid,
                name=names.get(uid, "Former member"),
                role=roles.get(uid),
                messages=a.get("messages", 0),
                conversations=a.get("conversations", 0),
                handovers=n,
                handovers_answered=answered,
                handover_median_seconds=median,
                leads_won=a.get("leads_won", 0),
            )
        )
    agents.sort(key=lambda a: (-a.messages, -a.handovers, a.name))

    fr = times.first_reply
    see_cost = ctx.can(Permission.AI_CONFIGURE) or ctx.can(Permission.BILLING_READ)
    return Overview(
        days=days,
        timezone=w.tz,
        start=w.start,
        end=w.end,
        totals=TotalsOut(**vars(current)),
        previous=TotalsOut(**vars(previous)),
        daily=[DayOut(**d) for d in await service.daily_messages(s, tid, w)],
        hours=[HourOut(**h) for h in await service.busiest_hours(s, tid, w)],
        handover_reasons=[ReasonOut(**r) for r in await service.handover_reasons(s, tid, w)],
        response=ResponseOut(
            first_reply_ai=ReplyTime(**fr["ai"]) if "ai" in fr else None,
            first_reply_team=ReplyTime(**fr["agent"]) if "agent" in fr else None,
            first_reply_none=fr.get("none", {}).get("conversations", 0),
            handovers=times.handover_total,
            handovers_answered=times.handover_answered,
            handover_median_seconds=times.handover_median_seconds,
        ),
        agents=agents,
        won_value=[MoneyOut(**v) for v in await service.won_value(s, tid, w)],
        open_unanswered=await service.open_unanswered(s, tid),
        delivery=DeliveryOut(**await service.delivery(s, tid, w)),
        ai_cost=CostOut(**await service.ai_cost(s, tid, w)) if see_cost else None,
    )


# ---- setup progress -------------------------------------------------------------------


class SetupOut(BaseModel):
    whatsapp_connected: bool
    assistant_published: bool
    auto_reply_on: bool
    knowledge_documents: int
    active_members: int


@router.get("/setup", response_model=SetupOut)
async def setup(ctx: Annotated[TenantContext, Requires(Permission.TENANT_READ)]) -> SetupOut:
    """What the business has set up so far, for the checklist on the overview. Every
    member may see it; it reveals only yes/no and counts."""
    s, tid = ctx.session, ctx.tenant_id
    connected = await s.scalar(
        select(func.count()).where(
            WhatsAppNumber.tenant_id == tid, WhatsAppNumber.status == NumberStatus.CONNECTED
        )
    )
    assistant = (await s.execute(select(Assistant).where(Assistant.tenant_id == tid))).scalar_one_or_none()
    docs = await s.scalar(
        select(func.count()).where(
            KnowledgeDocument.tenant_id == tid, KnowledgeDocument.active_version.is_not(None)
        )
    )
    members = await s.scalar(
        select(func.count()).where(TenantMembership.tenant_id == tid, TenantMembership.status == "active")
    )
    return SetupOut(
        whatsapp_connected=bool(connected),
        assistant_published=assistant is not None and assistant.active_version is not None,
        auto_reply_on=assistant is not None and assistant.auto_reply_enabled,
        knowledge_documents=int(docs or 0),
        active_members=int(members or 0),
    )
