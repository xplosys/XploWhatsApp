from __future__ import annotations

import httpx
import pyotp

from tests.conftest import CSRF, PASSWORD, Actor, ClientFactory, RegisterFn, token_from_outbox

API = "/api/v1"


async def test_register_creates_owner_tenant_and_trial(register: RegisterFn) -> None:
    owner = await register("owner@acme.in", "Acme Clinic")
    assert owner.tenant_id is not None
    me = (await owner.get(f"{API}/auth/me")).json()
    assert me["role"] == "owner"
    assert "billing:manage" in me["permissions"]
    assert me["user"]["email_verified"] is False
    sub = (await owner.get(f"{API}/billing/subscription")).json()
    assert sub["status"] == "trialing" and sub["plan_code"] == "trial"
    assert sub["seats_used"] == 1 and sub["seats_included"] == 3
    # Refresh token is only a cookie, never in the body.
    assert "refresh_token" not in owner.session
    assert owner.client.cookies.get("xplo_refresh")


async def test_register_validation(client: httpx.AsyncClient, register: RegisterFn) -> None:
    await register("dup@acme.in")
    r = await client.post(
        f"{API}/auth/register",
        json={"email": "dup@acme.in", "password": PASSWORD, "full_name": "X", "business_name": "B"},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"
    r = await client.post(
        f"{API}/auth/register",
        json={"email": "weak@acme.in", "password": "short", "full_name": "X", "business_name": "B"},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "weak_password"
    r = await client.post(
        f"{API}/auth/register", json={"email": "nobiz@acme.in", "password": PASSWORD, "full_name": "X"}
    )
    assert r.json()["error"]["code"] == "business_name_required"


async def test_tenant_id_in_body_is_ignored(register: RegisterFn) -> None:
    a = await register("a@one.in", "One")
    b = await register("b@two.in", "Two")
    r = await a.put(
        f"{API}/tenants/current", json={"name": "Renamed", "id": b.tenant_id, "tenant_id": b.tenant_id}
    )
    assert r.status_code == 200 and r.json()["id"] == a.tenant_id
    assert (await b.get(f"{API}/tenants/current")).json()["name"] == "Two"


async def test_login_and_lockout(client: httpx.AsyncClient, register: RegisterFn) -> None:
    await register("lock@acme.in")
    r = await client.post(f"{API}/auth/login", json={"email": "lock@acme.in", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["active_tenant_id"]
    for _ in range(5):
        r = await client.post(
            f"{API}/auth/login", json={"email": "lock@acme.in", "password": "wrong-password"}
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_credentials"
    r = await client.post(f"{API}/auth/login", json={"email": "lock@acme.in", "password": PASSWORD})
    assert r.status_code == 429 and "Retry-After" in r.headers


async def test_unknown_email_gets_same_error(client: httpx.AsyncClient) -> None:
    r = await client.post(f"{API}/auth/login", json={"email": "ghost@acme.in", "password": PASSWORD})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"


async def test_refresh_rotation_and_reuse_detection(register: RegisterFn, make_client: ClientFactory) -> None:
    actor = await register("rot@acme.in")
    first_cookie = actor.client.cookies.get("xplo_refresh")

    r = await actor.client.post(f"{API}/auth/refresh")
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_check_failed"

    r = await actor.client.post(f"{API}/auth/refresh", headers=CSRF)
    assert r.status_code == 200
    second_cookie = actor.client.cookies.get("xplo_refresh")
    assert second_cookie and second_cookie != first_cookie

    # An attacker replays the first (already rotated) refresh token.
    attacker = make_client()
    attacker.cookies.set("xplo_refresh", first_cookie, path="/api/v1/auth")
    r = await attacker.post(f"{API}/auth/refresh", headers=CSRF)
    assert r.status_code == 401 and r.json()["error"]["code"] == "refresh_token_reused"

    # The whole family is revoked, so the legitimate holder is signed out too.
    r = await actor.client.post(f"{API}/auth/refresh", headers=CSRF)
    assert r.status_code == 401


async def test_logout_revokes_session(register: RegisterFn) -> None:
    actor = await register("out@acme.in")
    cookie = actor.client.cookies.get("xplo_refresh")
    r = await actor.client.post(f"{API}/auth/logout", headers=CSRF)
    assert r.status_code == 204
    actor.client.cookies.set("xplo_refresh", cookie, path="/api/v1/auth")
    r = await actor.client.post(f"{API}/auth/refresh", headers=CSRF)
    assert r.status_code == 401


async def test_email_verification(register: RegisterFn) -> None:
    actor = await register("verify@acme.in")
    token = token_from_outbox("/verify-email?token=")
    r = await actor.client.post(f"{API}/auth/verify-email", json={"token": token})
    assert r.status_code == 200
    assert (await actor.get(f"{API}/auth/me")).json()["user"]["email_verified"] is True
    r = await actor.client.post(f"{API}/auth/verify-email", json={"token": token})
    assert r.json()["error"]["code"] == "invalid_or_expired_token"


async def test_password_reset_revokes_existing_sessions(
    register: RegisterFn, client: httpx.AsyncClient
) -> None:
    actor = await register("reset@acme.in")
    r = await client.post(f"{API}/auth/password/forgot", json={"email": "reset@acme.in"})
    assert r.status_code == 202
    r = await client.post(f"{API}/auth/password/forgot", json={"email": "nobody@acme.in"})
    assert r.status_code == 202  # no account enumeration
    token = token_from_outbox("/reset-password?token=")
    import asyncio

    await asyncio.sleep(1.1)  # access tokens have 1-second iat resolution
    r = await client.post(f"{API}/auth/password/reset", json={"token": token, "password": "New-Password-77!"})
    assert r.status_code == 200
    # Old access token and refresh token no longer work.
    assert (await actor.get(f"{API}/auth/me")).json()["error"]["code"] == "token_revoked"
    assert (await actor.client.post(f"{API}/auth/refresh", headers=CSRF)).status_code == 401
    r = await client.post(
        f"{API}/auth/login", json={"email": "reset@acme.in", "password": "New-Password-77!"}
    )
    assert r.status_code == 200


async def test_change_password(register: RegisterFn) -> None:
    actor = await register("change@acme.in")
    r = await actor.post(
        f"{API}/auth/password/change",
        json={"current_password": "nope-nope-nope", "new_password": "Another-Pass-99!"},
    )
    assert r.status_code == 401
    r = await actor.post(
        f"{API}/auth/password/change", json={"current_password": PASSWORD, "new_password": "Another-Pass-99!"}
    )
    assert r.status_code == 200
    actor.update(r.json())
    assert (await actor.get(f"{API}/auth/me")).status_code == 200


async def _enable_mfa(actor: Actor) -> tuple[str, list[str]]:
    setup = (await actor.post(f"{API}/auth/mfa/setup")).json()
    secret = setup["secret"]
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    r = await actor.post(f"{API}/auth/mfa/enable", json={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.text
    return secret, r.json()["recovery_codes"]


async def test_mfa_login_flow(register: RegisterFn, client: httpx.AsyncClient) -> None:
    actor = await register("mfa@acme.in")
    r = await actor.post(f"{API}/auth/mfa/enable", json={"code": "123456"})
    assert r.json()["error"]["code"] == "mfa_setup_missing"
    secret, recovery = await _enable_mfa(actor)

    r = await client.post(f"{API}/auth/login", json={"email": "mfa@acme.in", "password": PASSWORD})
    body = r.json()
    assert body["mfa_required"] is True and "access_token" not in body
    r = await client.post(f"{API}/auth/mfa/verify", json={"mfa_token": body["mfa_token"], "code": "000000"})
    assert r.status_code == 401
    r = await client.post(f"{API}/auth/login", json={"email": "mfa@acme.in", "password": PASSWORD})
    challenge = r.json()["mfa_token"]
    r = await client.post(
        f"{API}/auth/mfa/verify", json={"mfa_token": challenge, "code": pyotp.TOTP(secret).now()}
    )
    assert r.status_code == 200
    assert (
        await client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    ).json()["mfa_verified"]
    # Challenge tokens are single-use.
    r = await client.post(
        f"{API}/auth/mfa/verify", json={"mfa_token": challenge, "code": pyotp.TOTP(secret).now()}
    )
    assert r.json()["error"]["code"] == "mfa_token_used"

    # Recovery codes work once.
    challenge = (
        await client.post(f"{API}/auth/login", json={"email": "mfa@acme.in", "password": PASSWORD})
    ).json()["mfa_token"]
    assert (
        await client.post(f"{API}/auth/mfa/verify", json={"mfa_token": challenge, "code": recovery[0]})
    ).status_code == 200
    challenge = (
        await client.post(f"{API}/auth/login", json={"email": "mfa@acme.in", "password": PASSWORD})
    ).json()["mfa_token"]
    assert (
        await client.post(f"{API}/auth/mfa/verify", json={"mfa_token": challenge, "code": recovery[0]})
    ).status_code == 401


async def test_switch_tenant_requires_membership(register: RegisterFn) -> None:
    a = await register("switch-a@one.in", "One")
    b = await register("switch-b@two.in", "Two")
    r = await a.post(f"{API}/auth/switch-tenant", json={"tenant_id": b.tenant_id})
    assert r.status_code == 403 and r.json()["error"]["code"] == "not_a_member"


async def test_requests_without_valid_token(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"{API}/auth/me")).status_code == 401
    r = await client.get(f"{API}/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"
    assert r.json()["error"]["request_id"]


async def test_auth_rate_limit(register: RegisterFn, client: httpx.AsyncClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "rate_limit_auth_per_minute", 3)
    codes = [
        (await client.post(f"{API}/auth/password/forgot", json={"email": "x@acme.in"})).status_code
        for _ in range(4)
    ]
    assert codes == [202, 202, 202, 429]


async def test_health(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    r = await client.get("/health/ready")
    assert r.status_code == 200 and r.json()["checks"] == {"database": "ok", "redis": "ok"}
