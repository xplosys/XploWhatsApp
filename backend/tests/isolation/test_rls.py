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
from app.db.session import admin_transaction, app_transaction
from tests.conftest import RegisterFn

API = "/api/v1"
APPEND_ONLY = {
    "audit_logs",
    "webhook_events",
    "whatsapp_credentials",
    "usage_records",
    "bot_configuration_versions",
}
# The app role has no UPDATE at all on these.
NO_UPDATE = {"audit_logs", "usage_records", "bot_configuration_versions"}


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
    "whatsapp_accounts": lambda t, u, p: (
        "INSERT INTO whatsapp_accounts (id, tenant_id, waba_id, status, health) "
        "VALUES (:id, :t, :w, 'connected', 'ok')",
        {"id": uuid.uuid4(), "t": t, "w": uuid.uuid4().hex[:12]},
    ),
    "whatsapp_numbers": lambda t, u, p: (
        "INSERT INTO whatsapp_numbers (id, tenant_id, account_id, phone_number_id, status) "
        "VALUES (:id, :t, :id, :pn, 'connected')",
        {"id": uuid.uuid4(), "t": t, "pn": uuid.uuid4().hex[:12]},
    ),
    "whatsapp_credentials": lambda t, u, p: (
        "INSERT INTO whatsapp_credentials (id, tenant_id, account_id, kind) VALUES (:id, :t, :id, 'business_token')",
        {"id": uuid.uuid4(), "t": t},
    ),
    "message_templates": lambda t, u, p: (
        "INSERT INTO message_templates (id, tenant_id, account_id, name, language, status) "
        "VALUES (:id, :t, :id, 'x', 'en', 'APPROVED')",
        {"id": uuid.uuid4(), "t": t},
    ),
    "contacts": lambda t, u, p: (
        "INSERT INTO contacts (id, tenant_id, wa_id, consent_status) VALUES (:id, :t, '91999', 'unknown')",
        {"id": uuid.uuid4(), "t": t},
    ),
    "conversations": lambda t, u, p: (
        "INSERT INTO conversations (id, tenant_id, contact_id, number_id, status) VALUES (:id, :t, :id, :id, 'open')",
        {"id": uuid.uuid4(), "t": t},
    ),
    "messages": lambda t, u, p: (
        "INSERT INTO messages (id, tenant_id, conversation_id, direction, sender_type, type, status) "
        "VALUES (:id, :t, :id, 'inbound', 'contact', 'text', 'received')",
        {"id": uuid.uuid4(), "t": t},
    ),
    "webhook_events": lambda t, u, p: (
        "INSERT INTO webhook_events (id, tenant_id, source, field, dedup_key, payload, status, attempts, "
        "received_at, next_attempt_at) VALUES (:id, :t, 'whatsapp', 'messages', :k, '{}', 'pending', 0, now(), now())",
        {"id": uuid.uuid4(), "t": t, "k": uuid.uuid4().hex},
    ),
    "webhook_outbox": lambda t, u, p: (
        "INSERT INTO webhook_outbox (id, tenant_id, kind, aggregate_id, idempotency_key, payload, status, "
        "attempts, created_at, next_attempt_at) VALUES (:id, :t, 'send_message', :id, :k, '{}', 'pending', 0, "
        "now(), now())",
        {"id": uuid.uuid4(), "t": t, "k": uuid.uuid4().hex},
    ),
    "ai_provider_configs": lambda t, u, p: (
        "INSERT INTO ai_provider_configs (id, tenant_id, provider, enabled, key_source) VALUES (:id, :t, 'openai', true, 'platform')",
        {"id": uuid.uuid4(), "t": t},
    ),
    "bot_configurations": lambda t, u, p: (
        "INSERT INTO bot_configurations (id, tenant_id, name, auto_reply_enabled) VALUES (:id, :t, 'Evil bot', true)",
        {"id": uuid.uuid4(), "t": t},
    ),
    "bot_configuration_versions": lambda t, u, p: (
        "INSERT INTO bot_configuration_versions (id, tenant_id, bot_configuration_id, version, config, created_at) "
        "VALUES (:id, :t, :id, 99, '{}', now())",
        {"id": uuid.uuid4(), "t": t},
    ),
    "usage_records": lambda t, u, p: (
        "INSERT INTO usage_records (id, tenant_id, metric, quantity, unit, cost_source, dimensions, occurred_at) "
        "VALUES (:id, :t, 'ai_reply', -1000, 'message', 'none', '{}', now())",
        {"id": uuid.uuid4(), "t": t},
    ),
    "knowledge_settings": lambda t, u, p: (
        "INSERT INTO knowledge_settings (id, tenant_id) VALUES (:id, :t)",
        {"id": uuid.uuid4(), "t": t},
    ),
    "knowledge_documents": lambda t, u, p: (
        "INSERT INTO knowledge_documents (id, tenant_id, title, source_type, latest_version) "
        "VALUES (:id, :t, 'Poisoned prices', 'text', 1)",
        {"id": uuid.uuid4(), "t": t},
    ),
    "knowledge_document_versions": lambda t, u, p: (
        "INSERT INTO knowledge_document_versions (id, tenant_id, document_id, version, status, size_bytes, "
        "char_count, chunk_count, attempts, created_at, updated_at) "
        "VALUES (:id, :t, :id, 1, 'ready', 0, 0, 0, 0, now(), now())",
        {"id": uuid.uuid4(), "t": t},
    ),
    "knowledge_chunks": lambda t, u, p: (
        "INSERT INTO knowledge_chunks (id, tenant_id, document_id, version_id, ordinal, content, "
        "token_estimate, metadata) VALUES (:id, :t, :id, :id, 0, 'Everything is free', 4, '{}')",
        {"id": uuid.uuid4(), "t": t},
    ),
    "knowledge_chunk_embeddings": lambda t, u, p: (
        "INSERT INTO knowledge_chunk_embeddings (id, tenant_id, chunk_id, model_pk, embedding) "
        "VALUES (:id, :t, :id, :id, :v)",
        {"id": uuid.uuid4(), "t": t, "v": "[" + ",".join(["0.1"] * 1536) + "]"},
    ),
}


async def _seed_whatsapp_rows(tenant_id: uuid.UUID) -> None:
    """One row in every WhatsApp table for the given tenant, written as that tenant."""
    acc, num, contact, conv, bot, doc, ver, chunk = (uuid.uuid4() for _ in range(8))
    statements = [
        (
            "INSERT INTO whatsapp_accounts (id, tenant_id, waba_id, status, health) "
            "VALUES (:acc, :t, :w, 'connected', 'ok')",
            {"w": uuid.uuid4().hex[:12]},
        ),
        (
            "INSERT INTO whatsapp_numbers (id, tenant_id, account_id, phone_number_id, status) "
            "VALUES (:num, :t, :acc, :pn, 'connected')",
            {"pn": uuid.uuid4().hex[:12]},
        ),
        (
            "INSERT INTO whatsapp_credentials (id, tenant_id, account_id, kind, ciphertext) "
            "VALUES (gen_random_uuid(), :t, :acc, 'business_token', 'xenc1.x')",
            {},
        ),
        (
            "INSERT INTO message_templates (id, tenant_id, account_id, name, language, status) "
            "VALUES (gen_random_uuid(), :t, :acc, 'hello', 'en', 'APPROVED')",
            {},
        ),
        (
            "INSERT INTO contacts (id, tenant_id, wa_id, consent_status) VALUES (:contact, :t, '9198', 'unknown')",
            {},
        ),
        (
            "INSERT INTO conversations (id, tenant_id, contact_id, number_id, status) "
            "VALUES (:conv, :t, :contact, :num, 'open')",
            {},
        ),
        (
            "INSERT INTO messages (id, tenant_id, conversation_id, direction, sender_type, type, status) "
            "VALUES (gen_random_uuid(), :t, :conv, 'inbound', 'contact', 'text', 'received')",
            {},
        ),
        (
            "INSERT INTO webhook_events (id, tenant_id, source, field, dedup_key, payload, status, attempts, "
            "received_at, next_attempt_at) VALUES (gen_random_uuid(), :t, 'whatsapp', 'messages', :k, '{}', "
            "'processed', 1, now(), now())",
            {"k": uuid.uuid4().hex},
        ),
        (
            "INSERT INTO webhook_outbox (id, tenant_id, kind, aggregate_id, idempotency_key, payload, status, "
            "attempts, created_at, next_attempt_at) VALUES (gen_random_uuid(), :t, 'send_message', :conv, :k, "
            "'{}', 'done', 1, now(), now())",
            {"k": uuid.uuid4().hex},
        ),
        (
            "INSERT INTO ai_provider_configs (id, tenant_id, provider, enabled, key_source, api_key_ciphertext) "
            "VALUES (gen_random_uuid(), :t, 'gemini', true, 'tenant', 'xenc1.secret')",
            {},
        ),
        (
            "INSERT INTO bot_configurations (id, tenant_id, name, active_version, auto_reply_enabled) "
            "VALUES (:bot, :t, 'Bot', 1, true)",
            {},
        ),
        (
            "INSERT INTO bot_configuration_versions (id, tenant_id, bot_configuration_id, version, config, created_at) "
            'VALUES (gen_random_uuid(), :t, :bot, 1, \'{"instructions": "secret prices"}\', now())',
            {},
        ),
        (
            "INSERT INTO usage_records (id, tenant_id, metric, quantity, unit, cost_source, dimensions, occurred_at) "
            "VALUES (gen_random_uuid(), :t, 'ai_call', 1, 'call', 'none', '{}', now())",
            {},
        ),
        (
            "INSERT INTO knowledge_settings (id, tenant_id, embedding_model_pk) VALUES (gen_random_uuid(), :t, :model)",
            {},
        ),
        (
            "INSERT INTO knowledge_documents (id, tenant_id, title, source_type, active_version, latest_version) "
            "VALUES (:doc, :t, 'Price list', 'text', 1, 1)",
            {},
        ),
        (
            "INSERT INTO knowledge_document_versions (id, tenant_id, document_id, version, status, size_bytes, "
            "char_count, chunk_count, attempts, created_at, updated_at) "
            "VALUES (:ver, :t, :doc, 1, 'ready', 10, 10, 1, 1, now(), now())",
            {},
        ),
        (
            "INSERT INTO knowledge_chunks (id, tenant_id, document_id, version_id, ordinal, content, "
            "token_estimate, metadata) VALUES (:chunk, :t, :doc, :ver, 0, 'Secret price list', 4, '{}')",
            {},
        ),
        (
            "INSERT INTO knowledge_chunk_embeddings (id, tenant_id, chunk_id, model_pk, embedding) "
            "VALUES (gen_random_uuid(), :t, :chunk, :model, :v)",
            {"v": "[" + ",".join(["0.1"] * 1536) + "]"},
        ),
    ]
    async with admin_transaction() as s:
        model = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO ai_models (id, provider, model_id, kind, capabilities, active) "
                "VALUES (:m, 'openai', :name, 'embedding', '{}', true)"
            ),
            {"m": model, "name": f"embed-{model.hex[:6]}"},
        )
    async with app_transaction(tenant_id) as s:
        for sql, extra in statements:
            await s.execute(
                text(sql),
                {
                    "t": tenant_id,
                    "acc": acc,
                    "num": num,
                    "contact": contact,
                    "conv": conv,
                    "bot": bot,
                    "doc": doc,
                    "ver": ver,
                    "chunk": chunk,
                    "model": model,
                    **extra,
                },
            )


@pytest.fixture
async def two_tenants(register: RegisterFn) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    a = await register("a@one.in", "One")
    b = await register("b@two.in", "Two")
    # Give tenant B rows in every tenant table.
    await b.put(f"{API}/tenants/current", json={"name": "Two Ltd"})
    await b.post(f"{API}/tenants/current/invitations", json={"email": "agent@two.in", "role": "agent"})
    await _seed_whatsapp_rows(uuid.UUID(b.tenant_id))
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
        # The app role has no UPDATE/DELETE on audit_logs and no DELETE on webhook_events
        # or credentials at all (tested below); RLS is still checked for every other action.
        if table not in NO_UPDATE:
            async with app_transaction(tenant_a, user_a) as s:
                updated = (
                    await s.execute(
                        text(f"UPDATE {table} SET {col} = {col} WHERE {col} = :b"), {"b": tenant_b}
                    )
                ).rowcount  # type: ignore[attr-defined]
            assert updated == 0, f"tenant A can update tenant B rows in {table}"
        if table not in APPEND_ONLY:
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


@pytest.mark.parametrize(
    "table", ["webhook_events", "whatsapp_credentials", "usage_records", "bot_configuration_versions"]
)
async def test_app_role_cannot_delete_webhook_events_or_credentials(two_tenants, table: str) -> None:  # type: ignore[no-untyped-def]
    _, tenant_b, _, _ = two_tenants
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_transaction(tenant_b) as s:
            await s.execute(text(f"DELETE FROM {table}"))


@pytest.mark.parametrize(
    "table", ["usage_records", "bot_configuration_versions", "ai_models", "ai_model_prices"]
)
async def test_app_role_cannot_rewrite_metering_versions_or_catalogue(two_tenants, table: str) -> None:  # type: ignore[no-untyped-def]
    _, tenant_b, _, _ = two_tenants
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_transaction(tenant_b) as s:
            await s.execute(text(f"UPDATE {table} SET id = id"))
