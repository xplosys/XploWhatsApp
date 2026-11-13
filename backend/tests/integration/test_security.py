"""Phase 8: session control, the business two-factor requirement, rate limits, security
headers, and the personal data tools (erasure, export, business and account deletion,
retention)."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pyotp
import pytest
from sqlalchemy import text

from app.cli import _create_super_admin
from app.core.config import get_settings
from app.db.session import admin_transaction, app_transaction, drain_after_commit
from app.workers.dispatch import DATA_EXPORT, DISPATCHED
from tests.conftest import PASSWORD, Actor, RegisterFn
from tests.integration.test_ai import ai, clinic, meta, models  # noqa: F401
from tests.integration.test_inbox import deliver, join
from tests.support.meta import inbound_text

API = "/api/v1"
CSRF = {"X-Requested-With": "XploWhatsApp"}


async def login(client: httpx.AsyncClient, email: str) -> dict[str, Any]:
    r = await client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


async def enable_mfa(actor: Actor) -> str:
    """Turn on two-factor for an actor and move it to the resulting two-factor session."""
    secret = (await actor.post(f"{API}/auth/mfa/setup")).json()["secret"]
    r = await actor.post(f"{API}/auth/mfa/enable", json={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.text
    r = await actor.client.post(f"{API}/auth/refresh", headers=CSRF)
    assert r.status_code == 200, r.text
    actor.update(r.json())
    return secret


async def super_admin(make_client: Any) -> Actor:
    await _create_super_admin("root@xplosys.com", "Root", PASSWORD)
    c = make_client()
    actor = Actor(c, await login(c, "root@xplosys.com"))
    await enable_mfa(actor)
    return actor


# ---- sessions ---------------------------------------------------------------------------


async def test_sessions_can_be_listed_and_ended_remotely(register: RegisterFn, make_client: Any) -> None:
    owner = await register("o@acme.in", "Acme")
    laptop = make_client()
    other = Actor(laptop, await login(laptop, "o@acme.in"))
    phone = make_client()
    third = Actor(phone, await login(phone, "o@acme.in"))

    listed = (await owner.get(f"{API}/auth/sessions")).json()
    assert len(listed) == 3
    assert listed[0]["current"] and not any(s["current"] for s in listed[1:])

    # Ending one device stops its access token on the next request, not when it expires.
    laptop_session = next(s for s in listed if not s["current"])
    other_id = [s["id"] for s in listed if not s["current"]]
    r = await owner.delete(f"{API}/auth/sessions/{laptop_session['id']}")
    assert r.status_code == 204
    revoked = [a for a in (other, third) if (await a.get(f"{API}/auth/me")).status_code == 401]
    assert len(revoked) == 1
    assert (await revoked[0].get(f"{API}/auth/me")).json()["error"]["code"] == "session_revoked"
    # Its refresh cookie is dead too.
    assert (await revoked[0].client.post(f"{API}/auth/refresh", headers=CSRF)).status_code == 401

    # Someone else's session ID is not found.
    stranger = await register("x@other.in", "Other")
    r = await stranger.delete(f"{API}/auth/sessions/{other_id[-1]}")
    assert r.status_code == 404

    r = await owner.post(f"{API}/auth/sessions/revoke-others")
    assert r.status_code == 200
    for actor in (other, third):
        assert (await actor.get(f"{API}/auth/me")).status_code == 401
    assert (await owner.get(f"{API}/auth/me")).status_code == 200
    assert [s["current"] for s in (await owner.get(f"{API}/auth/sessions")).json()] == [True]

    # Signing out ends the access token at once.
    await owner.client.post(f"{API}/auth/logout", headers=CSRF)
    assert (await owner.get(f"{API}/auth/me")).status_code == 401

    async with admin_transaction() as s:
        actions = (await s.execute(text("SELECT action FROM audit_logs"))).scalars().all()
    assert "auth.session_revoked" in actions and "auth.sessions_revoked" in actions


async def test_business_can_require_two_factor(register: RegisterFn) -> None:
    owner = await register("o@acme.in", "Acme")
    agent = await join(register, owner, "a@acme.in", "agent")

    # The owner must use two-factor themselves first, or they would lock themselves out.
    r = await owner.put(f"{API}/tenants/current/security", json={"require_mfa": True})
    assert r.status_code == 403 and r.json()["error"]["code"] == "mfa_required"
    secret = await enable_mfa(owner)
    r = await owner.put(f"{API}/tenants/current/security", json={"require_mfa": True})
    assert r.status_code == 200 and r.json()["require_mfa"] and r.json()["members_without_mfa"] == 1
    # Admins can't change it; only the owner.
    assert (await agent.get(f"{API}/tenants/current/security")).status_code == 403

    # The agent is now blocked from the business until they turn it on.
    r = await agent.get(f"{API}/tenants/current")
    assert r.status_code == 403 and r.json()["error"]["code"] == "mfa_required_by_business"
    me = (await agent.get(f"{API}/auth/me")).json()
    assert me["mfa_required"] is True
    await enable_mfa(agent)
    assert (await agent.get(f"{API}/tenants/current")).status_code == 200
    assert (await agent.get(f"{API}/auth/me")).json()["mfa_required"] is False

    # Nobody can switch it off for themselves while the business requires it.
    r = await owner.post(
        f"{API}/auth/mfa/disable", json={"password": PASSWORD, "code": pyotp.TOTP(secret).now()}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "mfa_required_by_business"

    r = await owner.put(f"{API}/tenants/current/security", json={"message_retention_days": 10})
    assert r.status_code == 422
    r = await owner.put(f"{API}/tenants/current/security", json={"message_retention_days": 90})
    assert r.json()["message_retention_days"] == 90 and r.json()["require_mfa"]


# ---- rate limits and headers --------------------------------------------------------------


async def test_signed_in_requests_are_rate_limited_per_person(
    register: RegisterFn, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await register("o@acme.in", "Acme")
    other = await register("x@other.in", "Other")
    monkeypatch.setattr(get_settings(), "rate_limit_default_per_minute", 3)
    codes = [(await owner.get(f"{API}/auth/me")).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = await owner.get(f"{API}/auth/me")
    assert r.json()["error"]["code"] == "rate_limited" and int(r.headers["retry-after"]) >= 1
    # Another person has their own allowance.
    assert (await other.get(f"{API}/auth/me")).status_code == 200


async def test_webhooks_are_rate_limited_per_sender(
    client: httpx.AsyncClient, database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_webhook_per_minute", 2)
    codes = [(await client.post(f"{API}/webhooks/stripe", content=b"{}")).status_code for _ in range(3)]
    assert codes[-1] == 429 and 429 not in codes[:2]


async def test_forwarded_address_is_only_believed_from_a_trusted_proxy() -> None:
    from starlette.requests import Request

    from app.core.rate_limit import client_ip

    def request(peer: str, forwarded: str | None) -> Request:
        headers = [(b"x-real-ip", forwarded.encode())] if forwarded else []
        return Request({"type": "http", "headers": headers, "client": (peer, 1234)})

    assert client_ip(request("10.0.0.5", "203.0.113.9")) == "203.0.113.9"
    assert client_ip(request("198.51.100.7", "203.0.113.9")) == "198.51.100.7"
    assert client_ip(request("10.0.0.5", "not-an-ip")) == "10.0.0.5"
    assert client_ip(request("198.51.100.7", None)) == "198.51.100.7"


async def test_api_security_headers(client: httpx.AsyncClient, database: Any) -> None:
    r = await client.get("/health/live")
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in r.headers["permissions-policy"]
    assert r.headers["cache-control"] == "no-store"
    # HSTS only where the site is served over HTTPS.
    assert "strict-transport-security" not in r.headers

    from app.core.middleware import RequestContextMiddleware

    async def ok(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    wrapped = RequestContextMiddleware(ok, hsts_max_age=63072000)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://t") as c:
        r = await c.get("/")
    assert r.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"


# ---- erasure -----------------------------------------------------------------------------


async def test_contact_erasure_removes_their_data(
    clinic: Actor, register: RegisterFn, client: httpx.AsyncClient
) -> None:
    await deliver(client, inbound_text("Hi, my name is Priya", wamid="wamid.E1"))
    await deliver(client, inbound_text("I need a cleaning", wamid="wamid.E2"))
    await deliver(client, inbound_text("Other person", wamid="wamid.E3", wa_id="919800000002", bsuid=None))
    [priya] = [
        c for c in (await clinic.get(f"{API}/contacts")).json()["items"] if c["phone_e164"] == "+919812345678"
    ]
    lead = await clinic.post(f"{API}/leads", json={"contact_id": priya["id"], "title": "Cleaning"})
    assert lead.status_code == 201, lead.text

    agent = await join(register, clinic, "a@sunrisedental.in", "agent")
    assert (await agent.post(f"{API}/contacts/{priya['id']}/erase")).status_code == 403

    r = await clinic.post(f"{API}/contacts/{priya['id']}/erase")
    assert r.status_code == 200, r.text
    assert r.json() == {"conversations": 1, "messages": 2, "leads": 1}
    assert (await clinic.get(f"{API}/contacts/{priya['id']}")).status_code == 404
    assert (await clinic.get(f"{API}/contacts")).json()["total"] == 1
    async with app_transaction(uuid.UUID(clinic.tenant_id)) as s:
        left = await s.scalar(text("SELECT count(*) FROM messages WHERE body ILIKE '%priya%'"))
        assert left == 0
        row = (
            await s.execute(text("SELECT details FROM audit_logs WHERE action = 'contact.erased'"))
        ).scalar_one()
    assert row["messages"] == 2 and len(row["phone_fingerprint"]) == 32
    assert "919812345678" not in json.dumps(row)


# ---- export ------------------------------------------------------------------------------


async def run_exports() -> list[str]:
    from app.modules.privacy.service import build_export

    await drain_after_commit()
    results = []
    for job in [j for j in DISPATCHED if j.task == DATA_EXPORT]:
        DISPATCHED.remove(job)
        results.append(await build_export(uuid.UUID(job.args[0]), uuid.UUID(job.args[1])))
    await drain_after_commit()
    return results


async def test_business_data_export(clinic: Actor, register: RegisterFn, client: httpx.AsyncClient) -> None:
    from app.modules.notifications.email import OUTBOX
    from app.modules.storage import s3

    await deliver(client, inbound_text("Is the clinic open on Sunday?", wamid="wamid.X1"))
    admin = await join(register, clinic, "admin@sunrisedental.in", "admin")
    assert (await admin.post(f"{API}/privacy/exports")).status_code == 403

    r = await clinic.post(f"{API}/privacy/exports")
    assert r.status_code == 202 and r.json()["status"] == "queued"
    export_id = r.json()["id"]
    r = await clinic.post(f"{API}/privacy/exports")
    assert r.status_code == 409 and r.json()["error"]["code"] == "export_in_progress"
    r = await clinic.post(f"{API}/privacy/exports/{export_id}/download")
    assert r.json()["error"]["code"] == "export_not_ready"

    assert await run_exports() == ["ready"]
    [row] = (await clinic.get(f"{API}/privacy/exports")).json()
    assert row["status"] == "ready" and row["size_bytes"] > 0 and row["expires_at"]
    assert any("data export is ready" in m.subject for m in OUTBOX)
    r = await clinic.post(f"{API}/privacy/exports/{export_id}/download")
    assert r.status_code == 200 and "X-Amz-Signature" in r.json()["url"]

    async with app_transaction(uuid.UUID(clinic.tenant_id)) as s:
        key = await s.scalar(text("SELECT object_key FROM data_exports"))
    archive = zipfile.ZipFile(io.BytesIO(await s3.get(key, 50 * 1024 * 1024)))
    names = set(archive.namelist())
    assert {"tenants.jsonl", "contacts.jsonl", "messages.jsonl", "team_members.jsonl", "README.txt"} <= names
    # Secrets and search indexes stay out.
    assert "whatsapp_credentials.jsonl" not in names and "knowledge_embeddings.jsonl" not in names
    messages = [json.loads(line) for line in archive.read("messages.jsonl").splitlines()]
    assert any(m["body"] == "Is the clinic open on Sunday?" for m in messages)
    members = [json.loads(line)["email"] for line in archive.read("team_members.jsonl").splitlines()]
    assert sorted(members) == ["admin@sunrisedental.in", "owner@sunrisedental.in"]
    everything = b"".join(archive.read(n) for n in names)
    assert b"ciphertext" not in everything and b"token_hash" not in everything

    # Three a day.
    for _ in range(2):
        assert (await clinic.post(f"{API}/privacy/exports")).status_code == 202
        await run_exports()
    r = await clinic.post(f"{API}/privacy/exports")
    assert r.status_code == 409 and r.json()["error"]["code"] == "export_limit_reached"

    # After its date, the sweeper deletes the file and marks the export expired.
    from app.modules.privacy.sweeper import sweep

    async with admin_transaction() as s:
        await s.execute(text("UPDATE data_exports SET expires_at = now() - interval '1 minute'"))
    result = await sweep()
    assert result["exports_expired"] == 3
    assert not await s3.exists(key)
    rows = (await clinic.get(f"{API}/privacy/exports")).json()
    assert {r["status"] for r in rows} == {"expired"}


# ---- business deletion -------------------------------------------------------------------


async def test_business_deletion_restore_and_purge(
    clinic: Actor, register: RegisterFn, client: httpx.AsyncClient, make_client: Any
) -> None:
    from app.modules.privacy.sweeper import sweep
    from app.modules.storage import s3

    await deliver(client, inbound_text("Hello", wamid="wamid.D1"))
    agent = await join(register, clinic, "a@sunrisedental.in", "agent")
    tenant_id = uuid.UUID(clinic.tenant_id)
    await s3.put(f"tenants/{tenant_id}/knowledge/x/file", b"data", "text/plain")
    async with admin_transaction() as s:
        await s.execute(
            text(
                "INSERT INTO billing_invoices (id, tenant_id, number, currency, subtotal, tax_total, total, "
                "line_items, taxes, seller, buyer, gateway, gateway_payment_id, issued_at) VALUES "
                "(gen_random_uuid(), :t, 'XW/T/1', 'INR', 100, 18, 118, '[]', '[]', '{}', '{}', 'razorpay', "
                "'pay_1', now())"
            ),
            {"t": tenant_id},
        )

    body = {"password": PASSWORD, "confirm_name": "Sunrise"}
    r = await clinic.client.post(f"{API}/tenants/current/delete", json=body, headers=clinic.auth)
    assert r.json()["error"]["code"] == "csrf_check_failed"
    headers = {**clinic.auth, **CSRF}
    r = await clinic.client.post(f"{API}/tenants/current/delete", json=body, headers=headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "confirm_name_mismatch"
    body = {"password": "wrong-password", "confirm_name": "sunrise dental"}
    r = await clinic.client.post(f"{API}/tenants/current/delete", json=body, headers=headers)
    assert r.status_code == 401
    body["password"] = PASSWORD
    r = await agent.client.post(f"{API}/tenants/current/delete", json=body, headers={**agent.auth, **CSRF})
    assert r.status_code == 403

    r = await clinic.client.post(f"{API}/tenants/current/delete", json=body, headers=headers)
    assert r.status_code == 200, r.text
    purge_after = datetime.fromisoformat(r.json()["purge_after"])
    assert timedelta(days=29) < purge_after - datetime.now(UTC) <= timedelta(days=30)
    # Everyone loses access at once.
    r = await agent.get(f"{API}/tenants/current")
    assert r.status_code == 403 and r.json()["error"]["code"] == "not_a_member"
    assert (await agent.get(f"{API}/tenants")).json() == []

    # Support can restore it inside the grace period.
    root = await super_admin(make_client)
    [deleted] = (await root.get(f"{API}/admin/tenants-deleted")).json()
    assert deleted["name"] == "Sunrise Dental"
    r = await root.post(f"{API}/admin/tenants/{tenant_id}/restore")
    assert r.status_code == 200
    assert (await agent.get(f"{API}/tenants/current")).status_code == 200

    # Deleted again, the grace period runs out, and the sweeper purges it.
    login_again = make_client()
    owner = Actor(login_again, await login(login_again, "owner@sunrisedental.in"))
    r = await owner.client.post(f"{API}/tenants/current/delete", json=body, headers={**owner.auth, **CSRF})
    assert r.status_code == 200
    assert (await sweep())["businesses_purged"] == 0
    async with admin_transaction() as s:
        await s.execute(
            text("UPDATE tenants SET purge_after = now() - interval '1 minute' WHERE id = :t"),
            {"t": tenant_id},
        )
    assert (await sweep())["businesses_purged"] == 1
    async with admin_transaction() as s:
        tenant = (
            (await s.execute(text("SELECT * FROM tenants WHERE id = :t"), {"t": tenant_id})).mappings().one()
        )
        counts = {
            table: await s.scalar(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": tenant_id}
            )
            for table in (
                "contacts",
                "messages",
                "tenant_memberships",
                "whatsapp_credentials",
                "whatsapp_accounts",
                "billing_invoices",
                "audit_logs",
            )
        }
    assert tenant["name"] == "Deleted business" and tenant["purged_at"] is not None
    assert counts["contacts"] == counts["messages"] == counts["tenant_memberships"] == 0
    assert counts["whatsapp_credentials"] == counts["whatsapp_accounts"] == 0
    assert counts["billing_invoices"] == 1 and counts["audit_logs"] > 0
    assert not await s3.exists(f"tenants/{tenant_id}/knowledge/x/file")
    # Purged businesses can't be restored.
    r = await root.post(f"{API}/admin/tenants/{tenant_id}/restore")
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_purged"


async def test_paid_plan_must_be_cancelled_before_deleting(register: RegisterFn) -> None:
    owner = await register("o@acme.in", "Acme")
    async with admin_transaction() as s:
        await s.execute(
            text(
                "UPDATE subscriptions SET status = 'active', gateway = 'razorpay', "
                "gateway_subscription_id = 'sub_live' WHERE tenant_id = :t"
            ),
            {"t": uuid.UUID(owner.tenant_id)},
        )
    r = await owner.client.post(
        f"{API}/tenants/current/delete",
        json={"password": PASSWORD, "confirm_name": "Acme"},
        headers={**owner.auth, **CSRF},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "cancel_plan_first"


# ---- account deletion --------------------------------------------------------------------


async def test_account_deletion(register: RegisterFn, client: httpx.AsyncClient) -> None:
    owner = await register("o@acme.in", "Acme")
    agent = await join(register, owner, "a@acme.in", "agent")

    r = await owner.client.request(
        "DELETE", f"{API}/auth/me", json={"password": PASSWORD}, headers={**owner.auth, **CSRF}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "sole_owner"
    assert r.json()["error"]["details"]["businesses"] == ["Acme"]

    r = await agent.client.request(
        "DELETE", f"{API}/auth/me", json={"password": "nope"}, headers={**agent.auth, **CSRF}
    )
    assert r.status_code == 401
    r = await agent.client.request(
        "DELETE", f"{API}/auth/me", json={"password": PASSWORD}, headers={**agent.auth, **CSRF}
    )
    assert r.status_code == 204
    assert (await agent.get(f"{API}/auth/me")).status_code == 401
    r = await client.post(f"{API}/auth/login", json={"email": "a@acme.in", "password": PASSWORD})
    assert r.status_code == 401
    members = (await owner.get(f"{API}/tenants/current/members")).json()["items"]
    assert [m["email"] for m in members] == ["o@acme.in"]
    async with admin_transaction() as s:
        user = (
            (await s.execute(text("SELECT * FROM users WHERE id = :u"), {"u": uuid.UUID(agent.user_id)}))
            .mappings()
            .one()
        )
    assert user["full_name"] == "Deleted user" and user["email"].endswith("@deleted.invalid")
    assert user["deleted_at"] is not None and not user["is_active"]
    # The email address is free to sign up again.
    r = await client.post(
        f"{API}/auth/register",
        json={"email": "a@acme.in", "password": PASSWORD, "full_name": "Asha", "business_name": "New"},
    )
    assert r.status_code == 201


# ---- retention ---------------------------------------------------------------------------


async def test_message_retention_and_webhook_payload_cleanup(
    clinic: Actor, client: httpx.AsyncClient
) -> None:
    from app.modules.privacy.sweeper import sweep

    await deliver(client, inbound_text("Old message", wamid="wamid.R1"))
    await deliver(client, inbound_text("New message", wamid="wamid.R2"))
    tenant_id = uuid.UUID(clinic.tenant_id)
    async with admin_transaction() as s:
        await s.execute(
            text("UPDATE messages SET created_at = now() - interval '40 days' WHERE body = 'Old message'")
        )
        await s.execute(text("UPDATE webhook_events SET received_at = now() - interval '45 days'"))
    # No retention set: nothing is removed.
    result = await sweep()
    assert result["messages_removed"] == 0
    assert result["webhook_events_removed"] >= 2
    r = await clinic.put(f"{API}/tenants/current/security", json={"message_retention_days": 30})
    assert r.status_code == 200
    result = await sweep()
    assert result["messages_removed"] == 1
    async with app_transaction(tenant_id) as s:
        bodies = (await s.execute(text("SELECT body FROM messages"))).scalars().all()
    assert bodies == ["New message"]
