"""Phase 5: the agent workflow. Inbox views and search, assignment rules, taking over
from the assistant and handing back, closing, notes, contacts and tags, AI handover
records (history, unanswered questions, summaries) and leads."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.db.session import app_transaction, drain_after_commit
from app.modules.ai.models import UsageRecord
from app.modules.conversations.models import HandoverSession
from app.workers.dispatch import (
    AI_AUTO_REPLY,
    CONVERSATION_SUMMARY,
    DISPATCHED,
    PROCESS_WEBHOOK_EVENT,
    SEND_OUTBOX_JOB,
)
from tests.conftest import META_APP_SECRET, Actor, RegisterFn, token_from_outbox
from tests.integration.test_ai import (  # noqa: F401
    ai,
    clinic,
    config,
    meta,
    models,
    plan_limits,
    setup_assistant,
)
from tests.support.ai import FakeAI
from tests.support.meta import FakeMeta, inbound_text, signed

API = "/api/v1"


async def run_jobs() -> list[str]:
    from app.modules.ai.autoreply import reply
    from app.modules.conversations.outbound import send_job
    from app.modules.conversations.summary import summarise
    from app.modules.webhooks.processor import process_event

    await drain_after_commit()
    results = []
    while DISPATCHED:
        job = DISPATCHED.pop(0)
        ids = [uuid.UUID(x) for x in job.args]
        if job.task == PROCESS_WEBHOOK_EVENT:
            results.append(await process_event(ids[0], ids[1]))
        elif job.task == SEND_OUTBOX_JOB:
            results.append(await send_job(ids[0], ids[1]))
        elif job.task == AI_AUTO_REPLY:
            results.append(await reply(ids[0], ids[1], ids[2]))
        elif job.task == CONVERSATION_SUMMARY:
            results.append(f"summary:{await summarise(ids[0], ids[1])}")
        await drain_after_commit()
    return results


async def deliver(client: httpx.AsyncClient, payload: dict[str, Any]) -> list[str]:
    body, headers = signed(payload, META_APP_SECRET)
    r = await client.post(f"{API}/webhooks/whatsapp", content=body, headers=headers)
    assert r.status_code == 200
    return await run_jobs()


async def join(register: RegisterFn, owner: Actor, email: str, role: str) -> Actor:
    r = await owner.post(f"{API}/tenants/current/invitations", json={"email": email, "role": role})
    assert r.status_code == 201, r.text
    token = token_from_outbox("/accept-invite?token=")
    return await register(email, business=None, invitation_token=token)


async def only_conversation(actor: Actor) -> dict[str, Any]:
    [conv] = (await actor.get(f"{API}/conversations")).json()["items"]
    return conv


@pytest.fixture
async def team(register: RegisterFn, clinic: Actor, plan_limits: Any) -> dict[str, Actor]:
    await plan_limits(max_users=10)
    return {
        "owner": clinic,
        "agent": await join(register, clinic, "asha@sunrisedental.in", "agent"),
        "agent2": await join(register, clinic, "ravi@sunrisedental.in", "agent"),
        "supervisor": await join(register, clinic, "meera@sunrisedental.in", "supervisor"),
    }


# ---- inbox views, search and counts -------------------------------------------------


async def test_views_search_and_counts(clinic: Actor, client: httpx.AsyncClient) -> None:
    await deliver(client, inbound_text("Do you do teeth whitening?", wamid="wamid.A1", name="Priya"))
    await deliver(
        client,
        inbound_text(
            "Need a root canal appointment", wamid="wamid.B1", wa_id="919800000002", bsuid=None, name="Kabir"
        ),
    )
    page = (await clinic.get(f"{API}/conversations", params={"view": "open"})).json()
    assert page["total"] == 2
    assert all(c["awaiting_reply"] and c["assigned_to"] is None for c in page["items"])
    counts = (await clinic.get(f"{API}/conversations/counts")).json()
    assert counts == {
        "open": 2,
        "mine": 0,
        "unassigned": 2,
        "needs_human": 0,
        "awaiting": 2,
        "closed": 0,
        "unread": 2,
    }

    # Search by contact name, by phone digits, and by message text.
    by_name = (await clinic.get(f"{API}/conversations", params={"q": "kab"})).json()["items"]
    assert [c["contact"]["profile_name"] for c in by_name] == ["Kabir"]
    by_phone = (await clinic.get(f"{API}/conversations", params={"q": "9812345"})).json()["items"]
    assert [c["contact"]["profile_name"] for c in by_phone] == ["Priya"]
    by_text = (await clinic.get(f"{API}/conversations", params={"q": "WHITENING"})).json()["items"]
    assert [c["last_message_preview"] for c in by_text] == ["Do you do teeth whitening?"]
    # LIKE wildcards are matched literally.
    assert (await clinic.get(f"{API}/conversations", params={"q": "%%"})).json()["total"] == 0

    # An agent reply clears "awaiting reply".
    kabir = by_name[0]
    r = await clinic.post(f"{API}/conversations/{kabir['id']}/messages", json={"text": "Yes, tomorrow 11am?"})
    assert r.status_code == 202
    await run_jobs()
    awaiting = (await clinic.get(f"{API}/conversations", params={"view": "awaiting"})).json()["items"]
    assert [c["contact"]["profile_name"] for c in awaiting] == ["Priya"]

    # Closing moves it to the closed tab; a new customer message reopens it.
    r = await clinic.put(f"{API}/conversations/{kabir['id']}/status", json={"status": "closed"})
    assert r.json()["status"] == "closed" and r.json()["closed_at"] and r.json()["unread_count"] == 0
    assert (await clinic.get(f"{API}/conversations/counts")).json()["closed"] == 1
    await deliver(
        client,
        inbound_text("One more question", wamid="wamid.B2", wa_id="919800000002", bsuid=None, name="Kabir"),
    )
    reopened = (await clinic.get(f"{API}/conversations/{kabir['id']}")).json()
    assert reopened["status"] == "open" and reopened["closed_at"] is None and reopened["awaiting_reply"]


async def test_reactions_do_not_mark_a_conversation_as_awaiting_reply(
    clinic: Actor,
    client: httpx.AsyncClient,
) -> None:
    payload = inbound_text("x", wamid="wamid.R1")
    msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.pop("text")
    msg.update(type="reaction", reaction={"message_id": "wamid.OUT1", "emoji": "👍"})
    await deliver(client, payload)
    conv = await only_conversation(clinic)
    assert conv["awaiting_reply"] is False


# ---- assignment ---------------------------------------------------------------------


async def test_assignment_rules(team: dict[str, Actor], client: httpx.AsyncClient) -> None:
    owner, agent, agent2, sup = team["owner"], team["agent"], team["agent2"], team["supervisor"]
    await deliver(client, inbound_text("Hello"))
    conv_id = (await only_conversation(owner))["id"]
    url = f"{API}/conversations/{conv_id}/assignee"

    # An agent can pick up an unassigned conversation, but not give it to someone else.
    r = await agent.put(url, json={"user_id": agent2.user_id})
    assert r.status_code == 403 and r.json()["error"]["code"] == "assignment_not_allowed"
    r = await agent.put(url, json={"user_id": agent.user_id})
    assert r.status_code == 200 and r.json()["assigned_to"]["id"] == agent.user_id
    assert r.json()["assigned_to"]["name"] == "Asha"
    # Another agent cannot take it away.
    assert (await agent2.put(url, json={"user_id": agent2.user_id})).status_code == 403
    assert (await agent2.put(url, json={"user_id": None})).status_code == 403
    mine = (await agent.get(f"{API}/conversations", params={"view": "mine"})).json()
    assert mine["total"] == 1 and (await agent2.get(f"{API}/conversations/counts")).json()["mine"] == 0
    # A supervisor can reassign; not to someone outside the business.
    assert (await sup.put(url, json={"user_id": agent2.user_id})).json()["assigned_to"][
        "id"
    ] == agent2.user_id
    r = await sup.put(url, json={"user_id": str(uuid.uuid4())})
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_a_member"
    # The assignee can let go.
    assert (await agent2.put(url, json={"user_id": None})).json()["assigned_to"] is None
    filtered = (await owner.get(f"{API}/conversations", params={"view": "unassigned"})).json()
    assert filtered["total"] == 1

    # Removing a member returns their conversations and leads to the shared queue.
    await agent.put(url, json={"user_id": agent.user_id})
    lead = await agent.post(
        f"{API}/leads",
        json={"conversation_id": conv_id, "title": "Whitening", "owner_user_id": agent.user_id},
    )
    assert lead.status_code == 201, lead.text
    members = (await owner.get(f"{API}/tenants/current/members")).json()["items"]
    mid = next(m["id"] for m in members if m["email"] == "asha@sunrisedental.in")
    assert (await owner.delete(f"{API}/tenants/current/members/{mid}")).status_code == 204
    assert (await owner.get(f"{API}/conversations/{conv_id}")).json()["assigned_to"] is None
    assert (await owner.get(f"{API}/leads/{lead.json()['id']}")).json()["owner"] is None

    actions = [e["action"] for e in (await owner.get(f"{API}/audit-logs")).json()["items"]]
    assert "conversation.assigned" in actions and "conversation.unassigned" in actions


# ---- takeover and handover ---------------------------------------------------------


async def test_takeover_and_release(
    team: dict[str, Actor],
    client: httpx.AsyncClient,
    ai: FakeAI,
    meta: FakeMeta,
) -> None:
    owner, agent = team["owner"], team["agent"]
    await setup_assistant(owner, config())
    ai.reply("openai", "Hello! How can I help?")
    await deliver(client, inbound_text("Hi"))
    conv_id = (await only_conversation(owner))["id"]

    r = await agent.post(f"{API}/conversations/{conv_id}/takeover")
    body = r.json()
    assert body["ai_enabled"] is False and body["needs_human"] is False
    assert body["assigned_to"]["id"] == agent.user_id
    assert body["handover"]["started_by"] == "agent" and body["handover"]["agent"]["name"] == "Asha"
    # While a person has it, the assistant stays quiet.
    results = await deliver(client, inbound_text("Are you a bot?", wamid="wamid.IN0002"))
    assert "ai_off" in results or not any(r == "replied" for r in results)
    assert len(ai.calls("openai")) == 1

    r = await agent.post(f"{API}/conversations/{conv_id}/release")
    assert r.json()["ai_enabled"] is True and r.json()["handover"] is None
    history = (await owner.get(f"{API}/conversations/{conv_id}/handovers")).json()
    assert len(history) == 1 and history[0]["released_by"]["name"] == "Asha"
    assert history[0]["reason"] == "agent_takeover"

    # The old AI switch is the same takeover and release.
    await owner.put(f"{API}/conversations/{conv_id}/ai", json={"enabled": False})
    assert (await owner.get(f"{API}/conversations/{conv_id}")).json()["handover"]["reason"] == "ai_turned_off"
    await owner.put(f"{API}/conversations/{conv_id}/ai", json={"enabled": True})
    assert len((await owner.get(f"{API}/conversations/{conv_id}/handovers")).json()) == 2


async def test_ai_handover_records_question_and_writes_summary(
    clinic: Actor,
    client: httpx.AsyncClient,
    ai: FakeAI,
) -> None:
    await setup_assistant(clinic, config())
    ai.reply("openai", "I'll ask our team to call you back about the refund.", needs_human=True)
    ai.raw(
        "openai",
        json.dumps({"summary": "Priya wants a refund for yesterday's cleaning. Nobody has replied yet."}),
    )
    results = await deliver(client, inbound_text("I want a refund for yesterday's cleaning"))
    assert "handover" in results and "summary:ready" in results

    conv = await only_conversation(clinic)
    assert conv["needs_human"] and not conv["ai_enabled"]
    assert conv["handover"]["started_by"] == "ai" and conv["handover"]["reason"] == "needs_human"
    assert (await clinic.get(f"{API}/conversations/counts")).json()["needs_human"] == 1

    summary = (await clinic.get(f"{API}/conversations/{conv['id']}/summary")).json()
    assert summary["status"] == "ready" and summary["summary"].startswith("Priya wants a refund")
    assert summary["provider"] == "openai" and summary["message_count"] == 2 and summary["new_messages"] == 0
    # The transcript was sent as data with speaker labels.
    prompt = json.dumps(ai.calls("openai")[-1])
    assert "TRANSCRIPT START" in prompt and "Customer: I want a refund" in prompt
    assert "Assistant (AI): I'll ask our team" in prompt
    async with app_transaction(uuid.UUID(clinic.tenant_id)) as s:
        purposes = [u.dimensions.get("purpose") for u in (await s.execute(select(UsageRecord))).scalars()]
    assert "summary" in purposes

    unanswered = (await clinic.get(f"{API}/knowledge/unanswered")).json()
    assert unanswered["open"] == 1
    [q] = unanswered["items"]
    assert q["question"] == "I want a refund for yesterday's cleaning" and q["reason"] == "needs_human"
    assert q["contact_name"] == "Priya" and q["retrieval_hits"] == 0

    r = await clinic.patch(f"{API}/knowledge/unanswered/{q['id']}", json={"status": "resolved"})
    assert r.json()["status"] == "resolved" and r.json()["resolved_by"]["id"] == clinic.user_id
    assert (await clinic.get(f"{API}/knowledge/unanswered")).json()["open"] == 0
    assert (await clinic.get(f"{API}/knowledge/unanswered", params={"status": "all"})).json()["total"] == 1

    # A new message makes the summary stale; asking again writes a new one.
    await deliver(client, inbound_text("Hello??", wamid="wamid.IN0002"))
    assert (await clinic.get(f"{API}/conversations/{conv['id']}/summary")).json()["new_messages"] == 1
    ai.raw("openai", json.dumps({"summary": "Priya wants a refund and is waiting."}))
    r = await clinic.post(f"{API}/conversations/{conv['id']}/summary")
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert await run_jobs() == ["summary:ready"]
    again = (await clinic.get(f"{API}/conversations/{conv['id']}/summary")).json()
    assert again["summary"] == "Priya wants a refund and is waiting." and again["new_messages"] == 0


async def test_summary_failures_and_setting(clinic: Actor, client: httpx.AsyncClient, ai: FakeAI) -> None:
    await deliver(client, inbound_text("Hi"))
    conv_id = (await only_conversation(clinic))["id"]
    # Without an assistant there is no model to write it.
    await clinic.post(f"{API}/conversations/{conv_id}/summary")
    assert await run_jobs() == ["summary:failed"]
    s = (await clinic.get(f"{API}/conversations/{conv_id}/summary")).json()
    assert s["status"] == "failed" and "AI assistant" in s["error"]

    await setup_assistant(clinic, config(summary_on_handover=False))
    ai.raw("openai", "not json")
    await clinic.post(f"{API}/conversations/{conv_id}/summary")
    assert await run_jobs() == ["summary:failed"]
    assert (await clinic.get(f"{API}/conversations/{conv_id}/summary")).json()["error"] == (
        "The AI answer could not be read."
    )

    # With the setting off, a handover writes no summary but still records the question.
    ai.reply("openai", "Let me get someone.", needs_human=True)
    results = await deliver(client, inbound_text("Can I talk to the dentist?", wamid="wamid.IN0002"))
    assert "handover" in results and not any(r.startswith("summary") for r in results)
    assert (await clinic.get(f"{API}/knowledge/unanswered")).json()["open"] == 1


async def test_failed_models_hand_over_once(clinic: Actor, client: httpx.AsyncClient, ai: FakeAI) -> None:
    await setup_assistant(clinic, config(summary_on_handover=False))
    for _ in range(3):
        ai.fail("openai", 500, "upstream down")
    assert "handover" in await deliver(client, inbound_text("Hi, are you open on Sunday?"))
    async with app_transaction(uuid.UUID(clinic.tenant_id)) as s:
        sessions = list((await s.execute(select(HandoverSession))).scalars())
    assert len(sessions) == 1 and sessions[0].started_by == "ai" and sessions[0].reason == "all_failed"
    [q] = (await clinic.get(f"{API}/knowledge/unanswered")).json()["items"]
    assert q["reason"] == "all_failed" and q["question"] == "Hi, are you open on Sunday?"
    # A person taking over joins the open session instead of starting another.
    conv_id = (await only_conversation(clinic))["id"]
    r = await clinic.post(f"{API}/conversations/{conv_id}/takeover")
    assert r.json()["needs_human"] is False and r.json()["handover"]["agent"]["id"] == clinic.user_id
    assert len((await clinic.get(f"{API}/conversations/{conv_id}/handovers")).json()) == 1


# ---- notes ----------------------------------------------------------------------------


async def test_notes(team: dict[str, Actor], client: httpx.AsyncClient) -> None:
    owner, agent, agent2, sup = team["owner"], team["agent"], team["agent2"], team["supervisor"]
    await deliver(client, inbound_text("Hello"))
    conv_id = (await only_conversation(owner))["id"]
    url = f"{API}/conversations/{conv_id}/notes"

    r = await agent.post(url, json={"body": "  Regular patient, prefers mornings.  "})
    assert r.status_code == 201
    note = r.json()
    assert note["body"] == "Regular patient, prefers mornings." and note["author"]["name"] == "Asha"
    assert note["can_edit"] and note["can_delete"]
    assert (await agent.post(url, json={"body": "   "})).json()["error"]["code"] == "empty_note"

    seen = (await agent2.get(url)).json()
    assert len(seen) == 1 and not seen[0]["can_edit"] and not seen[0]["can_delete"]
    r = await agent2.patch(f"{url}/{note['id']}", json={"body": "changed"})
    assert r.status_code == 403
    assert (await agent2.delete(f"{url}/{note['id']}")).status_code == 403
    r = await agent.patch(f"{url}/{note['id']}", json={"body": "Prefers mornings before 11."})
    assert r.json()["body"] == "Prefers mornings before 11."
    # Notes never go to the customer.
    msgs = (await owner.get(f"{API}/conversations/{conv_id}/messages")).json()["items"]
    assert all("mornings" not in (m["body"] or "") for m in msgs)
    # A supervisor can delete anyone's note.
    assert (await sup.delete(f"{url}/{note['id']}")).status_code == 204
    assert (await owner.get(url)).json() == []


# ---- contacts --------------------------------------------------------------------------


async def test_contacts_tags_and_detail(team: dict[str, Actor], client: httpx.AsyncClient) -> None:
    owner, agent = team["owner"], team["agent"]
    await deliver(client, inbound_text("Hello", name="Priya"))
    await deliver(
        client, inbound_text("Hi", wamid="wamid.B1", wa_id="919800000002", bsuid=None, name="Kabir")
    )
    contacts = (await agent.get(f"{API}/contacts")).json()
    assert contacts["total"] == 2
    priya = next(c for c in contacts["items"] if c["profile_name"] == "Priya")

    r = await agent.patch(
        f"{API}/contacts/{priya['id']}",
        json={"name": "Priya Sharma", "language": "hi_IN", "tags": ["VIP", " Whitening ", "vip"]},
    )
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["name"] == "Priya Sharma" and detail["language"] == "hi-IN"
    assert detail["tags"] == ["vip", "whitening"]
    assert len(detail["conversations"]) == 1 and detail["conversations"][0]["number_display"]
    bad = await agent.patch(f"{API}/contacts/{priya['id']}", json={"tags": ["<script>"]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_tag"
    too_many = await agent.patch(f"{API}/contacts/{priya['id']}", json={"tags": [f"t{i}" for i in range(21)]})
    assert too_many.json()["error"]["code"] == "too_many_tags"

    assert (await agent.get(f"{API}/contacts/tags")).json() == [
        {"tag": "vip", "contacts": 1},
        {"tag": "whitening", "contacts": 1},
    ]
    assert (await agent.get(f"{API}/contacts", params={"tag": "VIP"})).json()["total"] == 1
    assert (await agent.get(f"{API}/contacts", params={"q": "sharma"})).json()["total"] == 1
    tagged = (await agent.get(f"{API}/conversations", params={"tag": "vip"})).json()["items"]
    assert [c["contact"]["name"] for c in tagged] == ["Priya Sharma"]
    assert (await agent.get(f"{API}/conversations", params={"q": "sharma"})).json()["total"] == 1

    # Clearing the name falls back to the WhatsApp profile name.
    r = await agent.patch(f"{API}/contacts/{priya['id']}", json={"name": "  "})
    assert r.json()["name"] is None and r.json()["profile_name"] == "Priya"
    actions = [e["action"] for e in (await owner.get(f"{API}/audit-logs")).json()["items"]]
    assert "contact.updated" in actions


# ---- leads ----------------------------------------------------------------------------


async def test_leads_pipeline(team: dict[str, Actor], client: httpx.AsyncClient) -> None:
    owner, agent, agent2, sup = team["owner"], team["agent"], team["agent2"], team["supervisor"]
    await deliver(client, inbound_text("How much are implants?"))
    conv = await only_conversation(owner)

    r = await agent.post(
        f"{API}/leads",
        json={
            "conversation_id": conv["id"],
            "title": "Dental implants",
            "value": "45000",
            "note": "Asked on WhatsApp",
        },
    )
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["source"] == "whatsapp" and lead["currency"] == "INR" and lead["value"] == "45000.00"
    assert lead["contact"]["name"] == "Priya" and lead["owner"] is None
    assert [a["type"] for a in lead["activities"]] in (["created", "note"], ["note", "created"])

    # Ownership follows the conversation rules.
    url = f"{API}/leads/{lead['id']}"
    r = await agent.patch(url, json={"owner_user_id": agent2.user_id})
    assert r.status_code == 403 and r.json()["error"]["code"] == "lead_owner_not_allowed"
    assert (await agent.patch(url, json={"owner_user_id": agent.user_id})).json()["owner"]["name"] == "Asha"
    assert (await agent2.patch(url, json={"owner_user_id": agent2.user_id})).status_code == 403
    assert (await sup.patch(url, json={"owner_user_id": agent2.user_id})).json()["owner"]["name"] == "Ravi"

    r = await agent2.patch(url, json={"status": "qualified", "value": "50000", "title": "Implants (2 teeth)"})
    body = r.json()
    assert body["status"] == "qualified" and body["value"] == "50000.00" and body["closed_at"] is None
    types = [a["type"] for a in body["activities"]]
    assert {"status_changed", "value_changed", "title_changed", "owner_changed"} <= set(types)
    status_change = next(a for a in body["activities"] if a["type"] == "status_changed")
    assert (
        status_change["data"] == {"from": "new", "to": "qualified"}
        and status_change["actor"]["name"] == "Ravi"
    )
    assert (await agent2.patch(url, json={"currency": "U5D"})).json()["error"]["code"] == "invalid_currency"

    r = await agent2.post(f"{url}/notes", json={"body": "Booked consultation for Monday"})
    assert (
        r.status_code == 201 and r.json()["activities"][0]["data"]["body"] == "Booked consultation for Monday"
    )

    manual = await owner.post(
        f"{API}/leads",
        json={"contact_id": conv["contact"]["id"], "title": "Whitening", "value": "120", "currency": "usd"},
    )
    assert manual.json()["source"] == "manual" and manual.json()["currency"] == "USD"
    summary = (await owner.get(f"{API}/leads/summary")).json()
    assert {s["status"]: s["count"] for s in summary["by_status"]} == {
        "new": 1,
        "contacted": 0,
        "qualified": 1,
        "won": 0,
        "lost": 0,
    }
    assert summary["open_value"] == [
        {"currency": "INR", "total": "50000.00"},
        {"currency": "USD", "total": "120.00"},
    ]
    assert summary["default_currency"] == "INR" and summary["unowned_open"] == 1

    r = await agent2.patch(url, json={"status": "won"})
    assert r.json()["closed_at"] is not None
    assert (await owner.get(f"{API}/leads", params={"status": "open"})).json()["total"] == 1
    assert (await owner.get(f"{API}/leads", params={"status": "closed"})).json()["total"] == 1
    assert (await agent2.get(f"{API}/leads", params={"owner": "me"})).json()["total"] == 1
    assert (await owner.get(f"{API}/leads", params={"q": "implants"})).json()["total"] == 1
    assert (await owner.get(f"{API}/leads", params={"q": "priya"})).json()["total"] == 2
    assert (await owner.get(f"{API}/leads", params={"owner": "bad"})).status_code == 422

    contact = (await owner.get(f"{API}/contacts/{conv['contact']['id']}")).json()
    assert {lead["title"] for lead in contact["leads"]} == {"Implants (2 teeth)", "Whitening"}

    # Only supervisors and above can delete; the history goes with it.
    assert (await agent2.delete(url)).status_code == 403
    assert (await sup.delete(url)).status_code == 204
    assert (await owner.get(url)).status_code == 404


async def test_lead_validation(clinic: Actor, register: RegisterFn, client: httpx.AsyncClient) -> None:
    other = await register("x@otherclinic.in", "Other Clinic")
    await deliver(client, inbound_text("Hello"))
    conv = await only_conversation(clinic)
    # Another business's conversation or contact is not found.
    r = await other.post(f"{API}/leads", json={"conversation_id": conv["id"], "title": "Steal"})
    assert r.status_code == 404
    r = await other.post(f"{API}/leads", json={"contact_id": conv["contact"]["id"], "title": "Steal"})
    assert r.status_code == 404
    assert (await clinic.post(f"{API}/leads", json={"title": "No one"})).status_code == 422
    r = await clinic.post(
        f"{API}/leads", json={"contact_id": conv["contact"]["id"], "title": "x", "value": "-1"}
    )
    assert r.status_code == 422
    lead = (
        await clinic.post(f"{API}/leads", json={"contact_id": conv["contact"]["id"], "title": "Braces"})
    ).json()
    assert (await other.get(f"{API}/leads/{lead['id']}")).status_code == 404
    assert (await other.get(f"{API}/leads")).json()["total"] == 0
    assert (await other.get(f"{API}/conversations/{conv['id']}/notes")).status_code == 404
    assert (await other.get(f"{API}/contacts/{conv['contact']['id']}")).status_code == 404


async def test_sweeper_resends_lost_summary_jobs(
    clinic: Actor, client: httpx.AsyncClient, su_conn: Any
) -> None:
    from app.modules.conversations.summary import sweep

    await deliver(client, inbound_text("Hi"))
    conv_id = (await only_conversation(clinic))["id"]
    await clinic.post(f"{API}/conversations/{conv_id}/summary")
    await drain_after_commit()
    DISPATCHED.clear()  # the job was lost
    assert await sweep() == 0  # too recent to count as lost
    await su_conn.execute("UPDATE conversation_summaries SET updated_at = now() - interval '10 minutes'")
    assert await sweep() == 1
    assert [(j.task, j.args[0]) for j in DISPATCHED] == [(CONVERSATION_SUMMARY, conv_id)]
    DISPATCHED.clear()
