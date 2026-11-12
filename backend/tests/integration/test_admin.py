from __future__ import annotations

import pyotp

from app.cli import _create_super_admin
from tests.conftest import PASSWORD, RegisterFn

API = "/api/v1"


async def test_super_admin_requires_mfa_and_can_suspend(register: RegisterFn, client) -> None:  # type: ignore[no-untyped-def]
    owner = await register("o@acme.in", "Acme")
    assert (await owner.get(f"{API}/admin/tenants")).status_code == 403

    await _create_super_admin("root@xplosys.com", "Root", PASSWORD)
    r = await client.post(f"{API}/auth/login", json={"email": "root@xplosys.com", "password": PASSWORD})
    token = r.json()["access_token"]
    assert r.json()["active_tenant_id"] is None
    h = {"Authorization": f"Bearer {token}"}
    r = await client.get(f"{API}/admin/tenants", headers=h)
    assert r.json()["error"]["code"] == "mfa_setup_required"

    secret = (await client.post(f"{API}/auth/mfa/setup", headers=h)).json()["secret"]
    await client.post(f"{API}/auth/mfa/enable", headers=h, json={"code": pyotp.TOTP(secret).now()})
    # Token from a password-only login is not enough.
    assert (await client.get(f"{API}/admin/tenants", headers=h)).json()["error"]["code"] == "mfa_required"
    r = await client.post(
        f"{API}/auth/mfa/disable", headers=h, json={"password": PASSWORD, "code": pyotp.TOTP(secret).now()}
    )
    assert r.json()["error"]["code"] == "mfa_required_for_role"

    challenge = (
        await client.post(f"{API}/auth/login", json={"email": "root@xplosys.com", "password": PASSWORD})
    ).json()["mfa_token"]
    r = await client.post(
        f"{API}/auth/mfa/verify", json={"mfa_token": challenge, "code": pyotp.TOTP(secret).now()}
    )
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    page = (await client.get(f"{API}/admin/tenants", headers=h)).json()
    assert page["total"] == 1
    row = page["items"][0]
    assert row["name"] == "Acme" and row["member_count"] == 1 and row["plan_code"] == "trial"

    r = await client.post(f"{API}/admin/tenants/{row['id']}/suspend", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    r = await owner.get(f"{API}/tenants/current")
    assert r.status_code == 403 and r.json()["error"]["code"] == "tenant_suspended"
    await client.post(f"{API}/admin/tenants/{row['id']}/reactivate", headers=h)
    assert (await owner.get(f"{API}/tenants/current")).status_code == 200
