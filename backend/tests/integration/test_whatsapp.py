"""WhatsApp integration: Embedded Signup onboarding, webhooks in, messages out,
status tracking, policy, health, templates and disconnect, against a fake Graph API."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.db.session import app_transaction, drain_after_commit
from app.modules.webhooks.models import WebhookEvent
from app.workers.dispatch import DISPATCHED, PROCESS_WEBHOOK_EVENT, SEND_OUTBOX_JOB
from tests.conftest import META_APP_ID, META_APP_SECRET, Actor, RegisterFn, token_from_outbox
from tests.support.meta import (
    BUSINESS_ID,
    BUSINESS_TOKEN,
    PHONE_NUMBER_ID,
    WABA_ID,
    FakeMeta,
    envelope,
    inbound_text,
    signed,
    status_update,
)

API = "/api/v1"


@pytest.fixture
def meta() -> Iterator[FakeMeta]:
    from app.modules.whatsapp.graph import set_transport_override

    fake = FakeMeta(app_id=META_APP_ID)
    set_transport_override(fake.transport)
    yield fake
    set_transport_override(None)


async def run_jobs() -> list[str]:
    """Run everything the API handed to workers, like Celery would."""
    from app.modules.conversations.outbound import send_job
    from app.modules.webhooks.processor import process_event

    await drain_after_commit()
    results = []
    while DISPATCHED:
        job = DISPATCHED.pop(0)
        a, b = (uuid.UUID(x) for x in job.args)
        if job.task == PROCESS_WEBHOOK_EVENT:
            results.append(await process_event(a, b))
        elif job.task == SEND_OUTBOX_JOB:
            results.append(await send_job(a, b))
        await drain_after_commit()
    return results


async def deliver(client: httpx.AsyncClient, payload: dict[str, Any]) -> httpx.Response:
    body, headers = signed(payload, META_APP_SECRET)
    r = await client.post(f"{API}/webhooks/whatsapp", content=body, headers=headers)
    await run_jobs()
    return r


async def connect(
    owner: Actor, *, event: str = "FINISH", phone_number_id: str | None = PHONE_NUMBER_ID
) -> dict[str, Any]:
    s = await owner.post(f"{API}/whatsapp/onboarding/sessions")
    assert s.status_code == 201, s.text
    body: dict[str, Any] = {
        "session_id": s.json()["session_id"],
        "code": "good-code",
        "waba_id": WABA_ID,
        "business_id": BUSINESS_ID,
        "event": event,
    }
    if phone_number_id:
        body["phone_number_id"] = phone_number_id
    r = await owner.post(f"{API}/whatsapp/onboarding/complete", json=body)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
async def connected(register: RegisterFn, meta: FakeMeta) -> tuple[Actor, dict[str, Any]]:
    owner = await register("owner@sunrisedental.in", "Sunrise Dental")
    account = await connect(owner)
    return owner, account


# ---- webhook verification and authentication ------------------------------------


async def test_webhook_verification_handshake(client: httpx.AsyncClient, database: Any) -> None:
    params = {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1158201444"}
    r = await client.get(f"{API}/webhooks/whatsapp", params=params)
    assert r.status_code == 200 and r.text == "1158201444"
    r = await client.get(f"{API}/webhooks/whatsapp", params={**params, "hub.verify_token": "nope"})
    assert r.status_code == 403


async def test_webhook_rejects_bad_or_missing_signature(client: httpx.AsyncClient, database: Any) -> None:
    payload = inbound_text("hi")
    body, _ = signed(payload, "wrong-secret")
    r = await client.post(
        f"{API}/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=00"}
    )
    assert r.status_code == 401
    r = await client.post(f"{API}/webhooks/whatsapp", content=body)
    assert r.status_code == 401
    async with app_transaction() as s:
        assert await s.scalar(text("SELECT count(*) FROM app_due_webhook_events(100, 0)")) == 0


async def test_unroutable_webhook_is_stored_but_not_processed(
    client: httpx.AsyncClient, su_conn: Any
) -> None:
    r = await deliver(client, inbound_text("hello", phone_number_id="999999999"))
    assert r.status_code == 200
    rows = await su_conn.fetch("SELECT status, tenant_id FROM webhook_events")
    assert [(r["status"], r["tenant_id"]) for r in rows] == [("unroutable", None)]
    assert DISPATCHED == []


# ---- onboarding ---------------------------------------------------------------


async def test_signup_config_reports_settings(register: RegisterFn) -> None:
    owner = await register("owner@sunrisedental.in")
    r = await owner.get(f"{API}/whatsapp/signup-config")
    assert r.status_code == 200
    assert r.json() | {"missing": []} == {
        "enabled": True,
        "app_id": META_APP_ID,
        "config_id": "cfg-123",
        "graph_api_version": "v25.0",
        "embedded_signup_version": "v4",
        "missing": [],
    }


async def test_embedded_signup_onboards_account(
    connected: tuple[Actor, dict[str, Any]], meta: FakeMeta, su_conn: Any
) -> None:
    owner, account = connected
    assert account["waba_id"] == WABA_ID and account["status"] == "connected"
    assert account["meta_business_id"] == BUSINESS_ID and account["name"] == "Sunrise Dental WABA"
    [number] = account["numbers"]
    assert number["phone_number_id"] == PHONE_NUMBER_ID and number["status"] == "connected"
    assert meta.subscribed and PHONE_NUMBER_ID in meta.registered
    assert [i["code"] for i in account["health_issues"]] == ["payment_method"]
    # Token and PIN are stored encrypted, never in plaintext.
    rows = await su_conn.fetch("SELECT kind, ciphertext FROM whatsapp_credentials WHERE revoked_at IS NULL")
    assert {r["kind"] for r in rows} == {"business_token", "registration_pin"}
    for r in rows:
        assert r["ciphertext"].startswith("xenc1.") and BUSINESS_TOKEN not in r["ciphertext"]
    # The owner can reveal the PIN Meta registered, and it is audited.
    pin = await owner.post(f"{API}/whatsapp/numbers/{number['id']}/pin")
    assert pin.json()["pin"] == meta.registered[PHONE_NUMBER_ID]
    actions = [r["action"] for r in await su_conn.fetch("SELECT action FROM audit_logs ORDER BY occurred_at")]
    assert "whatsapp.connected" in actions and "whatsapp.pin_viewed" in actions


async def test_onboarding_session_is_single_use_and_bound_to_user(
    register: RegisterFn, meta: FakeMeta
) -> None:
    owner = await register("owner@sunrisedental.in", "Sunrise Dental")
    other = await register("other@clinic.in", "Other Clinic")
    sid = (await owner.post(f"{API}/whatsapp/onboarding/sessions")).json()["session_id"]
    body = {"session_id": sid, "code": "good-code", "waba_id": WABA_ID, "phone_number_id": PHONE_NUMBER_ID}
    r = await other.post(f"{API}/whatsapp/onboarding/complete", json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "onboarding_mismatch"
    r = await owner.post(f"{API}/whatsapp/onboarding/complete", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "onboarding_expired"


async def test_onboarding_rejects_expired_code_and_missing_scopes(
    register: RegisterFn, meta: FakeMeta
) -> None:
    owner = await register("owner@sunrisedental.in")
    sid = (await owner.post(f"{API}/whatsapp/onboarding/sessions")).json()["session_id"]
    r = await owner.post(
        f"{API}/whatsapp/onboarding/complete", json={"session_id": sid, "code": "stale", "waba_id": WABA_ID}
    )
    assert r.status_code == 502 and r.json()["error"]["code"] == "meta_error"

    meta.scopes = ["whatsapp_business_management"]
    sid = (await owner.post(f"{API}/whatsapp/onboarding/sessions")).json()["session_id"]
    r = await owner.post(
        f"{API}/whatsapp/onboarding/complete",
        json={"session_id": sid, "code": "good-code", "waba_id": WABA_ID},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "scopes_missing"
    assert (await owner.get(f"{API}/whatsapp/accounts")).json() == []


async def test_onboarding_rejects_number_not_in_waba(register: RegisterFn, meta: FakeMeta) -> None:
    owner = await register("owner@sunrisedental.in")
    sid = (await owner.post(f"{API}/whatsapp/onboarding/sessions")).json()["session_id"]
    r = await owner.post(
        f"{API}/whatsapp/onboarding/complete",
        json={"session_id": sid, "code": "good-code", "waba_id": WABA_ID, "phone_number_id": "55555555"},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "number_not_in_waba"


async def test_same_waba_cannot_join_two_workspaces(
    connected: tuple[Actor, dict[str, Any]], register: RegisterFn
) -> None:
    intruder = await register("x@other.in", "Other")
    sid = (await intruder.post(f"{API}/whatsapp/onboarding/sessions")).json()["session_id"]
    r = await intruder.post(
        f"{API}/whatsapp/onboarding/complete",
        json={"session_id": sid, "code": "good-code", "waba_id": WABA_ID},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "waba_in_use"


async def test_onboarding_without_number_and_coexistence(register: RegisterFn, meta: FakeMeta) -> None:
    owner = await register("owner@sunrisedental.in")
    account = await connect(owner, phone_number_id=None)
    assert account["health"] == "warning"
    assert {i["code"] for i in account["health_issues"]} >= {"no_phone_number"}
    assert account["numbers"][0]["status"] == "pending"

    # Later the business finishes with a WhatsApp Business app number (coexistence).
    account = await connect(owner, event="FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING")
    number = account["numbers"][0]
    assert number["status"] == "connected" and number["is_coexistence"] is True
    assert PHONE_NUMBER_ID not in meta.registered  # never re-registered


async def test_agents_cannot_manage_whatsapp(
    connected: tuple[Actor, dict[str, Any]], register: RegisterFn
) -> None:
    owner, account = connected
    r = await owner.post(f"{API}/tenants/current/invitations", json={"email": "agent@x.in", "role": "agent"})
    token = token_from_outbox("/accept-invite?token=")
    agent = await register("agent@x.in", business=None, invitation_token=token)
    r = await agent.post(f"{API}/whatsapp/onboarding/sessions")
    assert r.status_code == 403
    # Agents can read the connection and reply, but not change it.
    assert (await agent.get(f"{API}/whatsapp/accounts")).status_code == 200
    assert (await agent.delete(f"{API}/whatsapp/accounts/{account['id']}")).status_code == 403


async def test_plan_limits_connected_numbers(register: RegisterFn, meta: FakeMeta) -> None:
    owner = await register("owner@sunrisedental.in")
    second = {**meta.phone_numbers[0], "id": "106540352242999", "display_phone_number": "+91 90000 00000"}
    meta.phone_numbers.append(second)
    account = await connect(owner)  # the trial plan allows one connected number
    assert {n["status"] for n in account["numbers"]} == {"connected", "pending"}
    sid = (await owner.post(f"{API}/whatsapp/onboarding/sessions")).json()["session_id"]
    r = await owner.post(
        f"{API}/whatsapp/onboarding/complete",
        json={"session_id": sid, "code": "good-code", "waba_id": WABA_ID, "phone_number_id": second["id"]},
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "plan_limit_reached"
    # Reconnecting the number that is already connected is always allowed.
    assert (await connect(owner))["status"] == "connected"


# ---- inbound messages ------------------------------------------------------------


async def test_inbound_message_creates_contact_conversation_and_message(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient
) -> None:
    owner, _ = connected
    r = await deliver(client, inbound_text("Do you have a slot tomorrow?"))
    assert r.status_code == 200
    convs = (await owner.get(f"{API}/conversations")).json()
    assert convs["total"] == 1
    conv = convs["items"][0]
    assert conv["contact"]["wa_id"] == "919812345678" and conv["contact"]["bsuid"] == "IN.1349120865530274"
    assert conv["contact"]["profile_name"] == "Priya" and conv["service_window_open"] is True
    assert conv["unread_count"] == 1 and conv["last_message_preview"] == "Do you have a slot tomorrow?"
    msgs = (await owner.get(f"{API}/conversations/{conv['id']}/messages")).json()["items"]
    assert [(m["direction"], m["body"], m["status"]) for m in msgs] == [
        ("inbound", "Do you have a slot tomorrow?", "received")
    ]


async def test_duplicate_webhooks_are_stored_once(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, su_conn: Any
) -> None:
    _owner, _ = connected
    payload = inbound_text("hello", wamid="wamid.DUP")
    await deliver(client, payload)
    await deliver(client, payload)  # exact redelivery
    # Same message, different envelope (Meta can batch differently on retry).
    await deliver(client, inbound_text("hello", wamid="wamid.DUP", name="Priya S"))
    assert await su_conn.fetchval("SELECT count(*) FROM messages") == 1
    assert await su_conn.fetchval("SELECT count(*) FROM webhook_events") == 2


async def test_username_only_contact_is_matched_by_bsuid(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient
) -> None:
    owner, _ = connected
    await deliver(client, inbound_text("first", wamid="wamid.A", wa_id=None, bsuid="IN.777"))
    await deliver(client, inbound_text("second", wamid="wamid.B", wa_id="919800000001", bsuid="IN.777"))
    convs = (await owner.get(f"{API}/conversations")).json()["items"]
    assert len(convs) == 1
    assert convs[0]["contact"]["bsuid"] == "IN.777" and convs[0]["contact"]["wa_id"] == "919800000001"


async def test_user_changed_user_id_moves_identifier(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient
) -> None:
    owner, _ = connected
    await deliver(client, inbound_text("hi", wamid="wamid.C", bsuid="IN.OLD"))
    system = envelope(
        "messages",
        {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "messages": [
                {
                    "id": "wamid.SYS",
                    "timestamp": str(int(time.time())),
                    "type": "system",
                    "system": {
                        "type": "user_changed_user_id",
                        "user_id": "IN.NEW",
                        "previous_user_id": "IN.OLD",
                    },
                }
            ],
        },
    )
    await deliver(client, system)
    contact = (await owner.get(f"{API}/conversations")).json()["items"][0]["contact"]
    assert contact["bsuid"] == "IN.NEW"


async def test_stop_keyword_opts_contact_out_and_blocks_sending(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient
) -> None:
    owner, _account = connected
    await deliver(client, inbound_text("STOP", wamid="wamid.STOP"))
    conv = (await owner.get(f"{API}/conversations")).json()["items"][0]
    assert conv["contact"]["consent_status"] == "opted_out"
    r = await owner.post(f"{API}/conversations/{conv['id']}/messages", json={"text": "Sorry to see you go"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "contact_opted_out"


# ---- outbound messages and statuses --------------------------------------------------


async def test_reply_inside_window_is_sent_and_tracked(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, meta: FakeMeta
) -> None:
    owner, _ = connected
    await deliver(client, inbound_text("Hello"))
    conv = (await owner.get(f"{API}/conversations")).json()["items"][0]
    r = await owner.post(f"{API}/conversations/{conv['id']}/messages", json={"text": "Yes, 10am is free."})
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert await run_jobs() == ["done"]
    assert meta.sent == [
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": "919812345678",
            "type": "text",
            "text": {"body": "Yes, 10am is free.", "preview_url": False},
        }
    ]
    msgs = (await owner.get(f"{API}/conversations/{conv['id']}/messages")).json()["items"]
    out = msgs[-1]
    assert out["status"] == "accepted"
    # Status webhooks, delivered out of order: read must not be overwritten by delivered.
    await deliver(client, status_update("wamid.OUT0001", "sent"))
    await deliver(client, status_update("wamid.OUT0001", "read"))
    await deliver(client, status_update("wamid.OUT0001", "delivered"))
    out = (await owner.get(f"{API}/conversations/{conv['id']}/messages")).json()["items"][-1]
    assert out["status"] == "read" and out["pricing_category"] == "service"


async def test_failed_status_records_error(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient
) -> None:
    owner, _ = connected
    await deliver(client, inbound_text("Hello"))
    conv = (await owner.get(f"{API}/conversations")).json()["items"][0]
    await owner.post(f"{API}/conversations/{conv['id']}/messages", json={"text": "hi"})
    await run_jobs()
    errors = [
        {"code": 131026, "title": "Message undeliverable", "error_data": {"details": "Recipient unavailable"}}
    ]
    await deliver(client, status_update("wamid.OUT0001", "failed", errors=errors))
    out = (await owner.get(f"{API}/conversations/{conv['id']}/messages")).json()["items"][-1]
    assert out["status"] == "failed" and out["error_code"] == "131026"
    assert out["error_message"] == "Recipient unavailable"


async def test_status_before_send_result_is_retried(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, su_conn: Any
) -> None:
    await deliver(client, status_update("wamid.UNKNOWN", "delivered"))
    row = await su_conn.fetchrow("SELECT status, attempts, last_error FROM webhook_events")
    assert row["status"] == "pending" and row["attempts"] == 1 and "not found" in row["last_error"]


async def test_free_form_outside_window_requires_template(
    connected: tuple[Actor, dict[str, Any]], meta: FakeMeta
) -> None:
    owner, account = connected
    number_id = account["numbers"][0]["id"]
    r = await owner.post(
        f"{API}/messages", json={"number_id": number_id, "phone": "+91 98123 45678", "text": "Hi!"}
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "outside_service_window" and err["details"]["requires_template"] is True
    assert meta.sent == []


async def test_template_send_and_policy(connected: tuple[Actor, dict[str, Any]], meta: FakeMeta) -> None:
    owner, account = connected
    meta.templates = [
        {
            "id": "111",
            "name": "appointment_reminder",
            "language": "en",
            "status": "APPROVED",
            "category": "UTILITY",
            "components": [{"type": "BODY", "text": "Hi {{1}}, your appointment is at {{2}}."}],
        },
        {
            "id": "222",
            "name": "diwali_offer",
            "language": "en",
            "status": "APPROVED",
            "category": "MARKETING",
            "components": [],
        },
        {
            "id": "333",
            "name": "new_offer",
            "language": "en",
            "status": "PENDING",
            "category": "MARKETING",
            "components": [],
        },
    ]
    r = await owner.post(f"{API}/whatsapp/accounts/{account['id']}/templates/sync")
    assert r.json() == {"synced": 3}
    templates = (await owner.get(f"{API}/whatsapp/templates", params={"approved_only": True})).json()
    assert sorted(t["name"] for t in templates) == ["appointment_reminder", "diwali_offer"]

    number_id = account["numbers"][0]["id"]
    components = [
        {"type": "body", "parameters": [{"type": "text", "text": "Priya"}, {"type": "text", "text": "10am"}]}
    ]
    r = await owner.post(
        f"{API}/messages",
        json={
            "number_id": number_id,
            "phone": "+919812345678",
            "template_name": "appointment_reminder",
            "template_language": "en",
            "template_components": components,
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["body"] == "Hi Priya, your appointment is at 10am."
    assert await run_jobs() == ["done"]
    assert meta.sent[-1]["template"] == {
        "name": "appointment_reminder",
        "language": {"code": "en"},
        "components": components,
    }

    # Marketing needs recorded opt-in; pending templates are refused.
    base = {"number_id": number_id, "phone": "+919812345678", "template_language": "en"}
    r = await owner.post(f"{API}/messages", json={**base, "template_name": "diwali_offer"})
    assert r.json()["error"]["code"] == "marketing_opt_in_required"
    r = await owner.post(f"{API}/messages", json={**base, "template_name": "new_offer"})
    assert r.json()["error"]["code"] == "template_not_approved"
    contact_id = (await owner.get(f"{API}/conversations")).json()["items"][0]["contact"]["id"]
    r = await owner.put(
        f"{API}/contacts/{contact_id}/consent", json={"status": "opted_in", "source": "clinic form"}
    )
    assert r.status_code == 200
    r = await owner.post(f"{API}/messages", json={**base, "template_name": "diwali_offer"})
    assert r.status_code == 202

    # Template status webhooks update the local copy.
    await _deliver_template_status(owner, "111", "PAUSED")
    templates = {t["name"]: t for t in (await owner.get(f"{API}/whatsapp/templates")).json()}
    assert templates["appointment_reminder"]["status"] == "PAUSED"


async def _deliver_template_status(owner: Actor, template_id: str, event: str) -> None:
    payload = envelope(
        "message_template_status_update",
        {
            "event": event,
            "message_template_id": int(template_id),
            "message_template_name": "appointment_reminder",
            "message_template_language": "en",
            "reason": None,
        },
    )
    await deliver(owner.client, payload)


async def test_send_retries_transient_errors_then_fails_permanent(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, meta: FakeMeta, su_conn: Any
) -> None:
    owner, _ = connected
    await deliver(client, inbound_text("Hello"))
    conv = (await owner.get(f"{API}/conversations")).json()["items"][0]
    meta.fail(r"POST /\d+/messages", 503, 131000, "Something went wrong")
    await owner.post(f"{API}/conversations/{conv['id']}/messages", json={"text": "one"})
    assert await run_jobs() == ["retry"]
    job = await su_conn.fetchrow(
        "SELECT status, attempts, next_attempt_at > now() AS later FROM webhook_outbox"
    )
    assert job["status"] == "pending" and job["attempts"] == 1 and job["later"]

    meta.fail(r"POST /\d+/messages", 400, 131026, "Message undeliverable")
    await owner.post(f"{API}/conversations/{conv['id']}/messages", json={"text": "two"})
    assert await run_jobs() == ["failed"]
    out = (await owner.get(f"{API}/conversations/{conv['id']}/messages")).json()["items"][-1]
    assert out["status"] == "failed" and out["error_code"] == "131026"


async def test_token_revoked_marks_account_needs_reconnect(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, meta: FakeMeta
) -> None:
    owner, account = connected
    await deliver(client, inbound_text("Hello"))
    conv = (await owner.get(f"{API}/conversations")).json()["items"][0]
    meta.fail(r"POST /\d+/messages", 401, 190, "Error validating access token")
    await owner.post(f"{API}/conversations/{conv['id']}/messages", json={"text": "one"})
    assert await run_jobs() == ["failed"]
    acc = (await owner.get(f"{API}/whatsapp/accounts/{account['id']}")).json()
    assert acc["status"] == "needs_reconnect"
    r = await owner.post(f"{API}/conversations/{conv['id']}/messages", json={"text": "two"})
    assert r.json()["error"]["code"] == "account_needs_reconnect"
    # Reconnecting through Embedded Signup restores the account.
    meta.failures.clear()
    acc = await connect(owner)
    assert acc["status"] == "connected"


async def test_stale_sending_job_is_not_resent(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, su_conn: Any
) -> None:
    from app.modules.webhooks.sweeper import sweep

    owner, _ = connected
    await deliver(client, inbound_text("Hello"))
    conv = (await owner.get(f"{API}/conversations")).json()["items"][0]
    await owner.post(f"{API}/conversations/{conv['id']}/messages", json={"text": "one"})
    await drain_after_commit()
    DISPATCHED.clear()
    # Simulate a worker that died after marking the job as sending.
    await su_conn.execute(
        "UPDATE webhook_outbox SET status='sending', started_at = now() - interval '1 hour'"
    )
    result = await sweep()
    assert result["stale_jobs"] == 1
    assert await su_conn.fetchval("SELECT status FROM webhook_outbox") == "unknown"
    assert (
        await su_conn.fetchval("SELECT error_code FROM messages WHERE direction='outbound'")
        == "send_outcome_unknown"
    )


async def test_sweeper_redispatches_pending_work(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, su_conn: Any
) -> None:
    from app.modules.webhooks.sweeper import sweep

    body, headers = signed(inbound_text("Hello"), META_APP_SECRET)
    await client.post(f"{API}/webhooks/whatsapp", content=body, headers=headers)
    DISPATCHED.clear()  # pretend the broker lost it
    await su_conn.execute("UPDATE webhook_events SET received_at = now() - interval '5 minutes'")
    assert (await sweep())["webhook_events"] == 1
    assert [j.task for j in DISPATCHED] == [PROCESS_WEBHOOK_EVENT]
    assert await run_jobs() == ["processed"]


# ---- account lifecycle -----------------------------------------------------------------


async def test_health_check_detects_revoked_access(
    connected: tuple[Actor, dict[str, Any]], meta: FakeMeta
) -> None:
    owner, account = connected
    r = await owner.post(f"{API}/whatsapp/accounts/{account['id']}/health-check")
    assert r.json()["health"] == "ok"
    meta.phone_numbers[0]["quality_rating"] = "RED"
    r = await owner.post(f"{API}/whatsapp/accounts/{account['id']}/health-check")
    assert r.json()["health"] == "warning" and r.json()["numbers"][0]["quality_rating"] == "RED"
    meta.token_valid = False
    r = await owner.post(f"{API}/whatsapp/accounts/{account['id']}/health-check")
    assert r.json()["status"] == "needs_reconnect"
    assert any(i["code"] == "token_invalid" for i in r.json()["health_issues"])


async def test_account_update_webhook_uninstall(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient
) -> None:
    owner, account = connected
    payload = envelope(
        "account_update", {"event": "PARTNER_APP_UNINSTALLED", "waba_info": {"waba_id": WABA_ID}}
    )
    await deliver(client, payload)
    acc = (await owner.get(f"{API}/whatsapp/accounts/{account['id']}")).json()
    assert acc["status"] == "needs_reconnect"


async def test_disconnect_unsubscribes_and_forgets_credentials(
    connected: tuple[Actor, dict[str, Any]], meta: FakeMeta, su_conn: Any
) -> None:
    owner, account = connected
    r = await owner.delete(f"{API}/whatsapp/accounts/{account['id']}")
    assert r.status_code == 200
    assert r.json()["meta_notified"] is True and r.json()["account"]["status"] == "disconnected"
    assert not meta.subscribed
    assert (
        await su_conn.fetchval("SELECT count(*) FROM whatsapp_credentials WHERE ciphertext IS NOT NULL") == 0
    )
    assert r.json()["account"]["numbers"][0]["status"] == "disconnected"


async def test_other_tenant_cannot_see_or_use_whatsapp_data(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, register: RegisterFn
) -> None:
    owner, account = connected
    await deliver(client, inbound_text("Hello"))
    conv = (await owner.get(f"{API}/conversations")).json()["items"][0]
    other = await register("x@other.in", "Other")
    assert (await other.get(f"{API}/whatsapp/accounts")).json() == []
    assert (await other.get(f"{API}/conversations")).json()["total"] == 0
    assert (await other.get(f"{API}/whatsapp/accounts/{account['id']}")).status_code == 404
    assert (await other.get(f"{API}/conversations/{conv['id']}/messages")).status_code == 404
    r = await other.post(
        f"{API}/messages",
        json={"number_id": account["numbers"][0]["id"], "phone": "+919812345678", "text": "x"},
    )
    assert r.status_code == 404
    r = await other.post(f"{API}/whatsapp/numbers/{account['numbers'][0]['id']}/pin")
    assert r.status_code == 404


async def test_webhook_rows_are_tenant_scoped(
    connected: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient
) -> None:
    owner, _ = connected
    await deliver(client, inbound_text("Hello"))
    async with app_transaction(uuid.UUID(owner.tenant_id)) as s:
        assert len((await s.execute(select(WebhookEvent))).scalars().all()) == 1
    async with app_transaction(uuid.uuid4()) as s:
        assert (await s.execute(select(WebhookEvent))).scalars().all() == []
