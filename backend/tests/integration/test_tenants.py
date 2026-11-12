from __future__ import annotations

from tests.conftest import CSRF, PASSWORD, Actor, ClientFactory, RegisterFn, token_from_outbox

API = "/api/v1"


async def _invite(owner: Actor, email: str, role: str) -> str:
    r = await owner.post(f"{API}/tenants/current/invitations", json={"email": email, "role": role})
    assert r.status_code == 201, r.text
    return token_from_outbox("/accept-invite?token=")


async def _join_new(register: RegisterFn, owner: Actor, email: str, role: str) -> Actor:
    token = await _invite(owner, email, role)
    member = await register(email, business=None, invitation_token=token)
    assert member.tenant_id == owner.tenant_id
    return member


async def test_get_and_update_tenant(register: RegisterFn) -> None:
    owner = await register("o@acme.in", "Acme")
    t = (await owner.get(f"{API}/tenants/current")).json()
    assert t["name"] == "Acme" and t["country"] == "IN" and t["timezone"] == "Asia/Kolkata"
    r = await owner.put(f"{API}/tenants/current", json={"name": "Acme Hospital", "industry": "healthcare"})
    assert r.status_code == 200 and r.json()["name"] == "Acme Hospital"
    r = await owner.put(f"{API}/tenants/current", json={"country": "india"})
    assert r.status_code == 422


async def test_invitation_signup_joins_tenant_with_role(register: RegisterFn, client) -> None:  # type: ignore[no-untyped-def]
    owner = await register("o@acme.in", "Acme")
    token = await _invite(owner, "agent@acme.in", "agent")
    preview = (await client.get(f"{API}/auth/invitations/{token}")).json()
    assert preview == {**preview, "tenant_name": "Acme", "email": "agent@acme.in", "role": "agent"}
    r = await client.post(
        f"{API}/auth/register",
        json={"email": "other@acme.in", "password": PASSWORD, "full_name": "X", "invitation_token": token},
    )
    assert r.json()["error"]["code"] == "invitation_email_mismatch"

    agent = await register("agent@acme.in", business=None, invitation_token=token)
    me = (await agent.get(f"{API}/auth/me")).json()
    assert me["role"] == "agent" and me["user"]["email_verified"] is True
    assert len(me["memberships"]) == 1  # no extra tenant was created
    # Invitation is single-use.
    assert (await client.get(f"{API}/auth/invitations/{token}")).status_code == 404


async def test_existing_user_accepts_invitation(register: RegisterFn) -> None:
    owner = await register("o@acme.in", "Acme")
    other = await register("x@other.in", "Other Biz")
    token = await _invite(owner, "x@other.in", "supervisor")
    r = await other.post(f"{API}/auth/invitations/accept", json={"token": token})
    assert r.json()["error"]["code"] == "email_not_verified"
    verify = token_from_outbox("/verify-email?token=")  # the newest verification mail is for x@other.in
    await other.client.post(f"{API}/auth/verify-email", json={"token": verify})
    r = await other.post(f"{API}/auth/invitations/accept", json={"token": token})
    assert r.status_code == 200, r.text
    other.update(r.json())
    assert other.tenant_id == owner.tenant_id
    assert {m["tenant_name"] for m in other.session["memberships"]} == {"Acme", "Other Biz"}
    assert (await other.get(f"{API}/auth/me")).json()["role"] == "supervisor"


async def test_permissions_by_role(register: RegisterFn) -> None:
    owner = await register("o@acme.in", "Acme")
    agent = await _join_new(register, owner, "agent@acme.in", "agent")
    assert (await agent.get(f"{API}/tenants/current")).status_code == 200
    assert (await agent.get(f"{API}/tenants/current/members")).status_code == 200
    r = await agent.put(f"{API}/tenants/current", json={"name": "Hacked"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
    assert (
        await agent.post(f"{API}/tenants/current/invitations", json={"email": "z@z.in", "role": "agent"})
    ).status_code == 403
    assert (await agent.get(f"{API}/audit-logs")).status_code == 403
    assert (await agent.get(f"{API}/billing/subscription")).status_code == 403


async def test_role_hierarchy_and_last_owner(register: RegisterFn) -> None:
    owner = await register("o@acme.in", "Acme")
    admin = await _join_new(register, owner, "admin@acme.in", "admin")
    members = (await owner.get(f"{API}/tenants/current/members")).json()["items"]
    by_email = {m["email"]: m for m in members}

    # Nobody can grant owner through the member API; admins cannot touch owners or admins.
    r = await owner.patch(
        f"{API}/tenants/current/members/{by_email['admin@acme.in']['id']}", json={"role": "owner"}
    )
    assert r.status_code == 403
    r = await admin.patch(
        f"{API}/tenants/current/members/{by_email['o@acme.in']['id']}", json={"role": "agent"}
    )
    assert r.status_code == 403
    r = await admin.post(f"{API}/tenants/current/invitations", json={"email": "a2@acme.in", "role": "admin"})
    assert r.status_code == 403
    r = await owner.delete(f"{API}/tenants/current/members/{by_email['o@acme.in']['id']}")
    assert r.json()["error"]["code"] == "cannot_modify_self"

    r = await owner.patch(
        f"{API}/tenants/current/members/{by_email['admin@acme.in']['id']}", json={"role": "supervisor"}
    )
    assert r.status_code == 200 and r.json()["role"] == "supervisor"
    # Role change applies to the very next request, without a new token.
    assert (await admin.get(f"{API}/auth/me")).json()["role"] == "supervisor"
    assert (await admin.get(f"{API}/audit-logs")).status_code == 403


async def test_removed_member_loses_access_immediately(register: RegisterFn) -> None:
    owner = await register("o@acme.in", "Acme")
    agent = await _join_new(register, owner, "agent@acme.in", "agent")
    members = (await owner.get(f"{API}/tenants/current/members")).json()["items"]
    agent_mid = next(m["id"] for m in members if m["email"] == "agent@acme.in")
    assert (await owner.delete(f"{API}/tenants/current/members/{agent_mid}")).status_code == 204
    r = await agent.get(f"{API}/tenants/current")
    assert r.status_code == 403 and r.json()["error"]["code"] == "not_a_member"
    # Their next refresh drops the tenant from the session.
    r = await agent.client.post(f"{API}/auth/refresh", headers=CSRF)
    assert r.status_code == 200 and r.json()["active_tenant_id"] is None


async def test_suspended_member(register: RegisterFn) -> None:
    owner = await register("o@acme.in", "Acme")
    agent = await _join_new(register, owner, "agent@acme.in", "agent")
    members = (await owner.get(f"{API}/tenants/current/members")).json()["items"]
    agent_mid = next(m["id"] for m in members if m["email"] == "agent@acme.in")
    await owner.patch(f"{API}/tenants/current/members/{agent_mid}", json={"status": "suspended"})
    r = await agent.get(f"{API}/tenants/current")
    assert r.json()["error"]["code"] == "membership_suspended"


async def test_seat_limit_counts_members_and_pending_invites(register: RegisterFn) -> None:
    owner = await register("o@acme.in", "Acme")  # trial plan: 3 seats
    await _invite(owner, "one@acme.in", "agent")
    await _invite(owner, "two@acme.in", "agent")
    r = await owner.post(
        f"{API}/tenants/current/invitations", json={"email": "three@acme.in", "role": "agent"}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "seat_limit_reached"
    pending = (await owner.get(f"{API}/tenants/current/invitations")).json()
    assert pending["total"] == 2
    inv_id = pending["items"][0]["id"]
    assert (await owner.delete(f"{API}/tenants/current/invitations/{inv_id}")).status_code == 204
    r = await owner.post(
        f"{API}/tenants/current/invitations", json={"email": "three@acme.in", "role": "agent"}
    )
    assert r.status_code == 201


async def test_audit_log_records_actions(register: RegisterFn) -> None:
    owner = await register("o@acme.in", "Acme")
    await owner.put(f"{API}/tenants/current", json={"name": "Acme 2"})
    await _invite(owner, "one@acme.in", "agent")
    logs = (await owner.get(f"{API}/audit-logs")).json()
    actions = [i["action"] for i in logs["items"]]
    assert {"tenant.created", "user.registered", "tenant.updated", "invitation.created"} <= set(actions)
    only = (await owner.get(f"{API}/audit-logs", params={"action": "tenant.updated"})).json()
    assert only["total"] == 1


async def test_pagination_bounds(register: RegisterFn) -> None:
    owner = await register("o@acme.in", "Acme")
    assert (await owner.get(f"{API}/tenants/current/members", params={"limit": 0})).status_code == 422
    assert (await owner.get(f"{API}/tenants/current/members", params={"limit": 101})).status_code == 422
    page = (await owner.get(f"{API}/tenants/current/members", params={"limit": 1})).json()
    assert page["total"] == 1 and page["limit"] == 1


async def test_my_tenants_lists_memberships(register: RegisterFn, make_client: ClientFactory) -> None:
    owner = await register("o@acme.in", "Acme")
    r = await owner.get(f"{API}/tenants")
    assert [m["tenant_name"] for m in r.json()] == ["Acme"]
