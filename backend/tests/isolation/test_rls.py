"""Tenant isolation is enforced by PostgreSQL itself, not only by application filters.

These tests talk to the database as the application role (xplo_app) and try to
read, change and create other tenants' rows directly. They also fail if a new
tenant-owned table is added without RLS, or without an insert case below.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.base import Base
from app.db.session import app_transaction
from tests.conftest import RegisterFn

API = "/api/v1"


def _tenant_tables() -> dict[str, str]:
    """{table: tenant column} for every model marked TenantScopedMixin."""
    import app.db.models  # noqa: F401

    out = {}
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if getattr(cls, "__tenant_scoped__", False):
            table = cls.__tablename__
            out[table] = "id" if table == "tenants" else "tenant_id"
    return out


async def test_every_table_with_tenant_id_is_marked_tenant_scoped(su_conn: asyncpg.Connection) -> None:
    rows = await su_conn.fetch(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema='public' AND column_name='tenant_id'"
    )
    with_column = {r["table_name"] for r in rows}
    # refresh_tokens stores the session's active tenant but belongs to the user, not the tenant.
    user_owned = {"refresh_tokens"}
    assert with_column - user_owned <= set(_tenant_tables()), "new tenant table is missing TenantScopedMixin"


async def test_rls_enabled_and_forced_on_all_tenant_tables(su_conn: asyncpg.Connection) -> None:
    rows = await su_conn.fetch(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
    )
    flags = {r["relname"]: (r["relrowsecurity"], r["relforcerowsecurity"]) for r in rows}
    for table in _tenant_tables():
        assert flags[table] == (True, True), f"{table} must ENABLE and FORCE row level security"
        policies = await su_conn.fetchval("SELECT count(*) FROM pg_policies WHERE tablename = $1", table)
        assert policies >= 1, f"{table} has no RLS policy"


async def test_app_role_cannot_bypass_rls(su_conn: asyncpg.Connection) -> None:
    role = await su_conn.fetchrow("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'xplo_app'")
    assert role["rolsuper"] is False and role["rolbypassrls"] is False
    owned = await su_conn.fetchval(
        "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner = 'xplo_app'"
    )
    assert owned == 0, "the application role must not own tables (owners can skip RLS)"


def _now() -> datetime:
    return datetime.now(UTC)


# One INSERT per tenant table, targeting a foreign tenant. Adding a tenant table
# without adding a case here makes test_cannot_insert_into_other_tenant fail.
INSERTS: dict[str, Callable[[uuid.UUID, uuid.UUID, uuid.UUID], tuple[str, dict[str, object]]]] = {
    "tenants": lambda t, u, p: (
        "INSERT INTO tenants (id, name, slug) VALUES (:t, 'Evil', :slug)",
        {"t": t, "slug": f"evil-{uuid.uuid4().hex[:6]}"},
    ),
    "tenant_memberships": lambda t, u, p: (
        "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) VALUES (:id, :t, :u, 'owner')",
        {"id": uuid.uuid4(), "t": t, "u": u},
    ),
    "tenant_invitations": lambda t, u, p: (
        "INSERT INTO tenant_invitations (id, tenant_id, email, role, token_hash, expires_at) "
        "VALUES (:id, :t, 'x@evil.in', 'admin', :h, :exp)",
        {"id": uuid.uuid4(), "t": t, "h": uuid.uuid4().hex, "exp": _now() + timedelta(days=1)},
    ),
    "subscriptions": lambda t, u, p: (
        "INSERT INTO subscriptions (id, tenant_id, plan_id, status) VALUES (:id, :t, :p, 'canceled')",
        {"id": uuid.uuid4(), "t": t, "p": p},
    ),
    "audit_logs": lambda t, u, p: (
        "INSERT INTO audit_logs (id, tenant_id, action, occurred_at) VALUES (:id, :t, 'forged', now())",
        {"id": uuid.uuid4(), "t": t},
    ),
}


@pytest.fixture
async def two_tenants(register: RegisterFn) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    a = await register("a@one.in", "One")
    b = await register("b@two.in", "Two")
    # Give tenant B rows in every tenant table.
    await b.put(f"{API}/tenants/current", json={"name": "Two Ltd"})
    await b.post(f"{API}/tenants/current/invitations", json={"email": "agent@two.in", "role": "agent"})
    return uuid.UUID(a.tenant_id), uuid.UUID(b.tenant_id), uuid.UUID(a.user_id), uuid.UUID(b.user_id)


async def test_tenant_cannot_see_or_change_other_tenant_rows(two_tenants) -> None:  # type: ignore[no-untyped-def]
    tenant_a, tenant_b, user_a, _ = two_tenants
    for table, col in _tenant_tables().items():
        # Sanity: B's data exists when acting as B.
        async with app_transaction(tenant_b) as s:
            own = await s.scalar(text(f"SELECT count(*) FROM {table} WHERE {col} = :b"), {"b": tenant_b})
        assert own and own > 0, f"fixture created no rows in {table}"

        async with app_transaction(tenant_a, user_a) as s:
            seen = await s.scalar(text(f"SELECT count(*) FROM {table} WHERE {col} = :b"), {"b": tenant_b})
        assert seen == 0, f"tenant A can read tenant B rows in {table}"
        if table != "audit_logs":  # app role has no UPDATE/DELETE on audit_logs at all (tested below)
            async with app_transaction(tenant_a, user_a) as s:
                updated = (
                    await s.execute(
                        text(f"UPDATE {table} SET {col} = {col} WHERE {col} = :b"), {"b": tenant_b}
                    )
                ).rowcount  # type: ignore[attr-defined]
            assert updated == 0, f"tenant A can update tenant B rows in {table}"
            async with app_transaction(tenant_a, user_a) as s:
                deleted = (
                    await s.execute(text(f"DELETE FROM {table} WHERE {col} = :b"), {"b": tenant_b})
                ).rowcount  # type: ignore[attr-defined]
            assert deleted == 0, f"tenant A can delete tenant B rows in {table}"


async def test_cannot_move_own_rows_into_other_tenant(two_tenants) -> None:  # type: ignore[no-untyped-def]
    tenant_a, tenant_b, user_a, _ = two_tenants
    with pytest.raises(DBAPIError, match="row-level security"):
        async with app_transaction(tenant_a, user_a) as s:
            await s.execute(
                text("UPDATE tenant_memberships SET tenant_id = :b WHERE tenant_id = :a"),
                {"a": tenant_a, "b": tenant_b},
            )


async def test_cannot_insert_into_other_tenant(two_tenants) -> None:  # type: ignore[no-untyped-def]
    tenant_a, tenant_b, user_a, _user_b = two_tenants
    assert set(INSERTS) == set(_tenant_tables()), "add an INSERTS case for each new tenant table"
    async with app_transaction() as s:
        plan_id = await s.scalar(text("SELECT id FROM subscription_plans LIMIT 1"))
    for table, build in INSERTS.items():
        target = uuid.uuid4() if table == "tenants" else tenant_b
        sql, params = build(target, user_a, plan_id)
        with pytest.raises(DBAPIError, match="row-level security"):
            async with app_transaction(tenant_a, user_a) as s:
                await s.execute(text(sql), params)


async def test_no_tenant_context_sees_nothing(two_tenants) -> None:  # type: ignore[no-untyped-def]
    for table in _tenant_tables():
        async with app_transaction(None) as s:
            assert await s.scalar(text(f"SELECT count(*) FROM {table}")) == 0, (
                f"{table} visible without context"
            )


async def test_tenant_context_does_not_leak_between_transactions(two_tenants) -> None:  # type: ignore[no-untyped-def]
    tenant_a, _, user_a, _ = two_tenants
    async with app_transaction(tenant_a, user_a) as s:
        assert await s.scalar(text("SELECT count(*) FROM tenants")) == 1
    # Same pool, new transaction: context was transaction-local.
    async with app_transaction(None) as s:
        assert await s.scalar(text("SELECT app_current_tenant_id()")) is None
        assert await s.scalar(text("SELECT count(*) FROM tenants")) == 0


async def test_audit_log_is_append_only_for_app_role(two_tenants) -> None:  # type: ignore[no-untyped-def]
    tenant_a, _, user_a, _ = two_tenants
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_transaction(tenant_a, user_a) as s:
            await s.execute(text("DELETE FROM audit_logs"))


async def test_plan_catalogue_is_read_only_for_app_role(two_tenants) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_transaction() as s:
            await s.execute(text("UPDATE subscription_plans SET name = 'free'"))


async def test_api_cannot_reach_other_tenant_resources_by_id(register: RegisterFn) -> None:
    a = await register("a@one.in", "One")
    b = await register("b@two.in", "Two")
    b_members = (await b.get(f"{API}/tenants/current/members")).json()["items"]
    inv = (
        await b.post(f"{API}/tenants/current/invitations", json={"email": "z@two.in", "role": "agent"})
    ).json()
    r = await a.patch(f"{API}/tenants/current/members/{b_members[0]['id']}", json={"role": "agent"})
    assert r.status_code == 404
    assert (await a.delete(f"{API}/tenants/current/members/{b_members[0]['id']}")).status_code == 404
    assert (await a.delete(f"{API}/tenants/current/invitations/{inv['id']}")).status_code == 404
    assert (await a.get(f"{API}/tenants/current/members")).json()["total"] == 1
    assert all(
        log["action"] != "invitation.created" for log in (await a.get(f"{API}/audit-logs")).json()["items"]
    )
