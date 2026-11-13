"""Phase 6: the analytics overview. Message volumes, the assistant's resolution rate,
response times, the team table, leads, AI cost visibility and permissions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import update

from app.db.session import app_transaction
from app.modules.analytics.service import window
from app.modules.conversations.models import Message
from tests.conftest import Actor, RegisterFn
from tests.integration.test_ai import (  # noqa: F401
    ai,
    clinic,
    config,
    meta,
    models,
    plan_limits,
    setup_assistant,
)
from tests.integration.test_inbox import deliver, join, run_jobs, team  # noqa: F401
from tests.support.ai import FakeAI
from tests.support.meta import inbound_text, status_update

API = "/api/v1"


def test_window_uses_the_business_time_zone() -> None:
    now = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)  # 01:30 on 14 Nov in India
    w = window(7, "Asia/Kolkata", now)
    assert str(w.first_day) == "2026-11-08"
    assert w.start == datetime(2026, 11, 7, 18, 30, tzinfo=UTC)
    prev = w.previous
    assert prev.end == w.start and str(prev.first_day) == "2026-11-01"
    assert window(7, "Not/AZone", now).tz == "UTC"


async def test_overview(
    team: dict[str, Actor],
    client: httpx.AsyncClient,
    ai: FakeAI,
) -> None:
    owner, agent, supervisor = team["owner"], team["agent"], team["supervisor"]
    await setup_assistant(owner, config())

    # Priya: the assistant answers and that's the end of it.
    ai.reply("openai", "We open at 9am, Monday to Saturday.")
    await deliver(
        client, inbound_text("When do you open?", wamid="wamid.P1", wa_id="919800000001", bsuid=None)
    )
    # Kabir: the assistant hands over and Asha answers.
    ai.reply("openai", "Let me get someone from the team.", needs_human=True)
    await deliver(
        client,
        inbound_text("I need a refund", wamid="wamid.K1", wa_id="919800000002", bsuid=None, name="Kabir"),
    )
    convs = (await owner.get(f"{API}/conversations")).json()["items"]
    kabir = next(c for c in convs if c["contact"]["profile_name"] == "Kabir")
    assert (await agent.post(f"{API}/conversations/{kabir['id']}/takeover")).status_code == 200
    r = await agent.post(
        f"{API}/conversations/{kabir['id']}/messages", json={"text": "Hi Kabir, I can help."}
    )
    assert r.status_code in (200, 201, 202), r.text
    await run_jobs()
    # A won lead for Kabir, owned by Asha.
    r = await agent.post(
        f"{API}/leads",
        json={
            "conversation_id": kabir["id"],
            "title": "Refund",
            "value": "1500",
            "owner_user_id": agent.user_id,
        },
    )
    assert r.status_code == 201, r.text
    assert (await agent.patch(f"{API}/leads/{r.json()['id']}", json={"status": "won"})).status_code == 200

    # Meta reports one reply read and one failed.
    await deliver(client, status_update("wamid.OUT0001", "read"))
    await deliver(
        client, status_update("wamid.OUT0002", "failed", errors=[{"code": 131047, "title": "Re-engagement"}])
    )

    # Agents cannot read analytics; supervisors can but don't see AI cost.
    assert (await agent.get(f"{API}/analytics/overview")).status_code == 403
    r = await supervisor.get(f"{API}/analytics/overview", params={"days": 7})
    assert r.status_code == 200, r.text
    sup = r.json()
    assert sup["ai_cost"] is None
    assert (await owner.get(f"{API}/analytics/overview", params={"days": 14})).status_code == 422

    o = (await owner.get(f"{API}/analytics/overview", params={"days": 7})).json()
    assert o["days"] == 7 and o["timezone"] == "Asia/Kolkata" and len(o["daily"]) == 7
    t = o["totals"]
    assert t["conversations_started"] == 2 and t["messages_in"] == 2
    assert t["messages_ai"] == 2 and t["messages_team"] == 1
    assert t["ai_conversations"] == 2 and t["ai_handovers"] == 1 and t["ai_resolution_rate"] == 0.5
    assert t["leads_created"] == 1 and t["leads_won"] == 1
    assert o["previous"]["messages_in"] == 0
    assert sum(d["customer"] for d in o["daily"]) == 2 and o["daily"][-1]["team"] == 1
    assert sum(h["messages"] for h in o["hours"]) == 2
    assert o["handover_reasons"] == [{"reason": "needs_human", "count": 1}]
    resp = o["response"]
    assert resp["first_reply_ai"]["conversations"] == 2 and resp["first_reply_team"] is None
    assert resp["handovers"] == 1 and resp["handovers_answered"] == 1
    assert resp["handover_median_seconds"] is not None
    [asha] = o["agents"]
    assert asha["user_id"] == agent.user_id and asha["role"] == "agent"
    assert asha["messages"] == 1 and asha["handovers"] == 1 and asha["leads_won"] == 1
    assert o["won_value"] == [{"currency": "INR", "total": "1500.00"}]
    assert o["open_unanswered"] == 1
    d = o["delivery"]
    assert d["read"] == 1 and d["failed"] == 1 and d["pending"] + d["sent"] + d["delivered"] == 1
    assert d["top_errors"] == [{"code": "131047", "count": 1}]
    assert d["billable"] == [{"category": "service", "messages": 1}]
    cost = o["ai_cost"]
    assert cost["calls"] >= 2 and {"purpose": "auto_reply", "calls": 2} in cost["calls_by_purpose"]

    # Messages from the previous week count in "previous", not in this period.
    async with app_transaction(uuid.UUID(owner.tenant_id)) as s:
        await s.execute(
            update(Message)
            .where(Message.body == "When do you open?")
            .values(created_at=datetime.now(UTC) - timedelta(days=9))
        )
    o2: dict[str, Any] = (await owner.get(f"{API}/analytics/overview", params={"days": 7})).json()
    assert o2["totals"]["messages_in"] == 1 and o2["previous"]["messages_in"] == 1


async def test_other_businesses_are_not_counted(
    clinic: Actor,
    register: RegisterFn,
    client: httpx.AsyncClient,
) -> None:
    await deliver(client, inbound_text("Hello"))
    other = await register("owner@othershop.in", business="Other Shop")
    o = (await other.get(f"{API}/analytics/overview")).json()
    assert o["totals"]["messages_in"] == 0 and o["agents"] == [] and o["open_unanswered"] == 0
    assert (await clinic.get(f"{API}/analytics/overview")).json()["totals"]["messages_in"] == 1


async def test_setup_progress(clinic: Actor, ai: FakeAI) -> None:
    s = (await clinic.get(f"{API}/analytics/setup")).json()
    assert s["whatsapp_connected"] is True and s["active_members"] == 1
    assert s["assistant_published"] is False and s["knowledge_documents"] == 0
    await setup_assistant(clinic, config())
    s = (await clinic.get(f"{API}/analytics/setup")).json()
    assert s["assistant_published"] is True and s["auto_reply_on"] is True
