"""Analytics queries. Every figure is computed from operational tables at request time,
inside the caller's tenant (row-level security still applies). Days and hours are in the
business's own time zone."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Date, and_, case, cast, distinct, extract, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.models import UsageRecord
from app.modules.conversations.models import (
    Conversation,
    Direction,
    HandoverSession,
    HandoverStarter,
    Message,
    SenderType,
    UnansweredQuestion,
    UnansweredStatus,
)
from app.modules.leads.models import Lead, LeadStatus

PERIODS = (7, 30, 90)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    tz: str
    days: int
    first_day: date

    @property
    def previous(self) -> Window:
        span = self.end - self.start
        return Window(
            self.start - span, self.start, self.tz, self.days, self.first_day - timedelta(days=self.days)
        )


def window(days: int, tz_name: str, now: datetime | None = None) -> Window:
    """The last `days` calendar days in the business's time zone, today included."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz_name, tz = "UTC", ZoneInfo("UTC")
    now = now or datetime.now(UTC)
    today = now.astimezone(tz).date()
    first = today - timedelta(days=days - 1)
    start = datetime(first.year, first.month, first.day, tzinfo=tz).astimezone(UTC)
    return Window(start=start, end=now, tz=tz_name, days=days, first_day=first)


def _local(col: Any, tz: str) -> Any:
    return func.timezone(tz, col)


@dataclass
class Totals:
    conversations_started: int = 0
    messages_in: int = 0
    messages_ai: int = 0
    messages_team: int = 0
    ai_conversations: int = 0
    ai_handovers: int = 0
    ai_resolution_rate: float | None = None
    leads_created: int = 0
    leads_won: int = 0


async def totals(session: AsyncSession, tenant_id: uuid.UUID, w: Window) -> Totals:
    t = Totals()
    in_range = and_(Message.tenant_id == tenant_id, Message.created_at >= w.start, Message.created_at < w.end)
    row = (
        await session.execute(
            select(
                func.count().filter(and_(Message.direction == Direction.INBOUND, Message.type != "reaction")),
                func.count().filter(Message.sender_type == SenderType.AI),
                func.count().filter(Message.sender_type == SenderType.AGENT),
                func.count(distinct(Message.conversation_id)).filter(Message.sender_type == SenderType.AI),
            ).where(in_range)
        )
    ).one()
    t.messages_in, t.messages_ai, t.messages_team, t.ai_conversations = (int(v or 0) for v in row)
    t.conversations_started = int(
        await session.scalar(
            select(func.count()).where(
                Conversation.tenant_id == tenant_id,
                Conversation.created_at >= w.start,
                Conversation.created_at < w.end,
            )
        )
        or 0
    )
    # Conversations the assistant answered in the period and later handed to a person.
    handed = select(HandoverSession.conversation_id).where(
        HandoverSession.tenant_id == tenant_id,
        HandoverSession.started_by != HandoverStarter.AGENT,
        HandoverSession.started_at >= w.start,
        HandoverSession.started_at < w.end,
    )
    t.ai_handovers = int(await session.scalar(select(func.count()).select_from(handed.subquery())) or 0)
    if t.ai_conversations:
        handed_after_ai = int(
            await session.scalar(
                select(func.count(distinct(Message.conversation_id))).where(
                    in_range, Message.sender_type == SenderType.AI, Message.conversation_id.in_(handed)
                )
            )
            or 0
        )
        t.ai_resolution_rate = round(1 - handed_after_ai / t.ai_conversations, 4)
    lead_row = (
        await session.execute(
            select(
                func.count().filter(and_(Lead.created_at >= w.start, Lead.created_at < w.end)),
                func.count().filter(
                    and_(Lead.status == LeadStatus.WON, Lead.closed_at >= w.start, Lead.closed_at < w.end)
                ),
            ).where(Lead.tenant_id == tenant_id)
        )
    ).one()
    t.leads_created, t.leads_won = int(lead_row[0] or 0), int(lead_row[1] or 0)
    return t


async def daily_messages(session: AsyncSession, tenant_id: uuid.UUID, w: Window) -> list[dict[str, Any]]:
    day = cast(_local(Message.created_at, w.tz), Date)
    rows = (
        await session.execute(
            select(
                day,
                func.count().filter(and_(Message.direction == Direction.INBOUND, Message.type != "reaction")),
                func.count().filter(Message.sender_type == SenderType.AI),
                func.count().filter(Message.sender_type == SenderType.AGENT),
            )
            .where(Message.tenant_id == tenant_id, Message.created_at >= w.start, Message.created_at < w.end)
            .group_by(day)
        )
    ).all()
    by_day = {r[0]: r for r in rows}
    out = []
    for i in range(w.days):
        d = w.first_day + timedelta(days=i)
        r = by_day.get(d)
        out.append(
            {
                "date": d,
                "customer": int(r[1]) if r else 0,
                "ai": int(r[2]) if r else 0,
                "team": int(r[3]) if r else 0,
            }
        )
    return out


async def busiest_hours(session: AsyncSession, tenant_id: uuid.UUID, w: Window) -> list[dict[str, int]]:
    """Customer messages by local weekday (1 Monday .. 7 Sunday) and hour."""
    local = _local(Message.created_at, w.tz)
    dow, hour = extract("isodow", local), extract("hour", local)
    rows = (
        await session.execute(
            select(dow, hour, func.count())
            .where(
                Message.tenant_id == tenant_id,
                Message.created_at >= w.start,
                Message.created_at < w.end,
                Message.direction == Direction.INBOUND,
                Message.type != "reaction",
            )
            .group_by(dow, hour)
        )
    ).all()
    return [{"weekday": int(d), "hour": int(h), "messages": int(c)} for d, h, c in rows]


async def handover_reasons(session: AsyncSession, tenant_id: uuid.UUID, w: Window) -> list[dict[str, Any]]:
    reason = case((HandoverSession.reason.like("blocked:%"), "blocked"), else_=HandoverSession.reason).label(
        "reason"
    )
    rows = (
        await session.execute(
            select(reason, func.count())
            .where(
                HandoverSession.tenant_id == tenant_id,
                HandoverSession.started_by != HandoverStarter.AGENT,
                HandoverSession.started_at >= w.start,
                HandoverSession.started_at < w.end,
            )
            .group_by(reason)
            .order_by(func.count().desc())
        )
    ).all()
    return [{"reason": r, "count": int(c)} for r, c in rows]


# Response times. The first reply after the customer's first message in a new conversation,
# and the time a person took to write after the assistant handed over.
_FIRST_REPLY = text(
    """
    WITH firsts AS (
        SELECT c.id, (SELECT min(m.created_at) FROM messages m
                      WHERE m.conversation_id = c.id AND m.direction = 'inbound') AS asked
        FROM conversations c
        WHERE c.tenant_id = :tenant AND c.created_at >= :start AND c.created_at < :end
    ), replies AS (
        SELECT f.id, f.asked, r.sender_type, r.created_at AS answered
        FROM firsts f
        LEFT JOIN LATERAL (
            SELECT m.sender_type, m.created_at FROM messages m
            WHERE m.conversation_id = f.id AND m.direction = 'outbound'
              AND m.sender_type IN ('ai', 'agent') AND m.created_at >= f.asked
            ORDER BY m.created_at LIMIT 1
        ) r ON true
        WHERE f.asked IS NOT NULL
    )
    SELECT sender_type, count(*),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM answered - asked)),
           percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM answered - asked))
    FROM replies GROUP BY sender_type
    """
)

_TIME_TO_PERSON = text(
    """
    SELECT GROUPING(h.agent_user_id) AS is_total, h.agent_user_id, count(*),
           count(r.created_at),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM r.created_at - h.started_at))
    FROM human_handover_sessions h
    LEFT JOIN LATERAL (
        SELECT m.created_at FROM messages m
        WHERE m.conversation_id = h.conversation_id AND m.sender_type = 'agent'
          AND m.created_at >= h.started_at
          AND (h.released_at IS NULL OR m.created_at <= h.released_at)
        ORDER BY m.created_at LIMIT 1
    ) r ON true
    WHERE h.tenant_id = :tenant AND h.started_by <> 'agent'
      AND h.started_at >= :start AND h.started_at < :end
    GROUP BY GROUPING SETS ((h.agent_user_id), ())
    """
)


@dataclass
class ResponseTimes:
    first_reply: dict[str, dict[str, Any]] = field(default_factory=dict)
    handover_total: int = 0
    handover_answered: int = 0
    handover_median_seconds: float | None = None
    per_agent: dict[uuid.UUID, tuple[int, int, float | None]] = field(default_factory=dict)


def _num(v: Any) -> float | None:
    return None if v is None else round(float(v), 1)


async def response_times(session: AsyncSession, tenant_id: uuid.UUID, w: Window) -> ResponseTimes:
    params = {"tenant": tenant_id, "start": w.start, "end": w.end}
    out = ResponseTimes()
    for sender, count, p50, p90 in (await session.execute(_FIRST_REPLY, params)).all():
        key = sender or "none"
        out.first_reply[key] = {
            "conversations": int(count),
            "median_seconds": _num(p50),
            "p90_seconds": _num(p90),
        }
    for is_total, agent, n, answered, p50 in (await session.execute(_TIME_TO_PERSON, params)).all():
        if is_total:
            out.handover_total, out.handover_answered = int(n), int(answered)
            out.handover_median_seconds = _num(p50)
        elif agent is not None:
            out.per_agent[agent] = (int(n), int(answered), _num(p50))
    return out


async def team_activity(
    session: AsyncSession, tenant_id: uuid.UUID, w: Window
) -> dict[uuid.UUID, dict[str, int]]:
    rows = (
        await session.execute(
            select(Message.sent_by_user_id, func.count(), func.count(distinct(Message.conversation_id)))
            .where(
                Message.tenant_id == tenant_id,
                Message.sender_type == SenderType.AGENT,
                Message.sent_by_user_id.is_not(None),
                Message.created_at >= w.start,
                Message.created_at < w.end,
            )
            .group_by(Message.sent_by_user_id)
        )
    ).all()
    out: dict[uuid.UUID, dict[str, int]] = {
        uid: {"messages": int(m), "conversations": int(c)} for uid, m, c in rows if uid is not None
    }
    won = (
        await session.execute(
            select(Lead.owner_user_id, func.count())
            .where(
                Lead.tenant_id == tenant_id,
                Lead.owner_user_id.is_not(None),
                Lead.status == LeadStatus.WON,
                Lead.closed_at >= w.start,
                Lead.closed_at < w.end,
            )
            .group_by(Lead.owner_user_id)
        )
    ).all()
    for uid, n in won:
        if uid is not None:
            out.setdefault(uid, {"messages": 0, "conversations": 0})["leads_won"] = int(n)
    return out


async def won_value(session: AsyncSession, tenant_id: uuid.UUID, w: Window) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Lead.currency, func.sum(Lead.value))
            .where(
                Lead.tenant_id == tenant_id,
                Lead.status == LeadStatus.WON,
                Lead.closed_at >= w.start,
                Lead.closed_at < w.end,
                Lead.value.is_not(None),
                Lead.currency.is_not(None),
            )
            .group_by(Lead.currency)
            .order_by(Lead.currency)
        )
    ).all()
    return [{"currency": c, "total": v} for c, v in rows]


async def open_unanswered(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count()).where(
                UnansweredQuestion.tenant_id == tenant_id, UnansweredQuestion.status == UnansweredStatus.OPEN
            )
        )
        or 0
    )


async def ai_cost(session: AsyncSession, tenant_id: uuid.UUID, w: Window) -> dict[str, Any]:
    purpose = UsageRecord.dimensions["purpose"].astext
    rows = (
        await session.execute(
            select(purpose, UsageRecord.currency, func.count(), func.sum(UsageRecord.cost_estimate))
            .where(
                UsageRecord.tenant_id == tenant_id,
                UsageRecord.metric == "ai_call",
                UsageRecord.occurred_at >= w.start,
                UsageRecord.occurred_at < w.end,
            )
            .group_by(purpose, UsageRecord.currency)
        )
    ).all()
    by_currency: dict[str, Decimal] = {}
    by_purpose: dict[str, int] = {}
    calls = unpriced = 0
    for p, currency, n, cost in rows:
        calls += int(n)
        by_purpose[p or "other"] = by_purpose.get(p or "other", 0) + int(n)
        if currency and cost is not None:
            by_currency[currency] = by_currency.get(currency, Decimal(0)) + cost
        else:
            unpriced += int(n)
    return {
        "calls": calls,
        "unpriced_calls": unpriced,
        "cost": [{"currency": c, "total": round(v, 4)} for c, v in sorted(by_currency.items())],
        "calls_by_purpose": [{"purpose": p, "calls": n} for p, n in sorted(by_purpose.items())],
    }


async def delivery(session: AsyncSession, tenant_id: uuid.UUID, w: Window) -> dict[str, Any]:
    """Where the messages sent in the period ended up, from Meta's status webhooks, and
    the conversation categories Meta marked as billable. Meta bills the business
    directly; this counts, it does not price (no rate card is stored)."""
    in_range = (
        Message.tenant_id == tenant_id,
        Message.direction == Direction.OUTBOUND,
        Message.created_at >= w.start,
        Message.created_at < w.end,
    )
    by_status = dict(
        (
            await session.execute(
                select(Message.status, func.count()).where(*in_range).group_by(Message.status)
            )
        ).all()
    )
    pending = sum(int(by_status.get(s, 0)) for s in ("queued", "sending", "accepted"))
    errors = (
        await session.execute(
            select(Message.error_code, func.count())
            .where(*in_range, Message.status == "failed")
            .group_by(Message.error_code)
            .order_by(func.count().desc())
            .limit(5)
        )
    ).all()
    billable = (
        await session.execute(
            select(Message.pricing_category, func.count())
            .where(*in_range, Message.billable.is_(True))
            .group_by(Message.pricing_category)
            .order_by(Message.pricing_category)
        )
    ).all()
    return {
        "sent": int(by_status.get("sent", 0)),
        "delivered": int(by_status.get("delivered", 0)),
        "read": int(by_status.get("read", 0)),
        "failed": int(by_status.get("failed", 0)),
        "pending": pending,
        "top_errors": [{"code": c or "unknown", "count": int(n)} for c, n in errors],
        "billable": [{"category": c or "unknown", "messages": int(n)} for c, n in billable],
    }
