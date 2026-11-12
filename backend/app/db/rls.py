"""SQL helpers used by migrations to put a table under tenant RLS."""

from __future__ import annotations

from app.db.roles import APP_ROLE

CURRENT_TENANT_FN = """
CREATE OR REPLACE FUNCTION app_current_tenant_id() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE
AS $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$;
"""


def enable_tenant_rls(table: str, column: str = "tenant_id", allow_null_insert: bool = False) -> list[str]:
    """Statements that enable and force RLS with a fail-closed tenant policy.

    If app.tenant_id is unset, app_current_tenant_id() is NULL and nothing matches.
    """
    check = f"{column} = app_current_tenant_id()"
    if allow_null_insert:
        check = f"({column} IS NULL OR {check})"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_isolation ON {table} USING ({column} = app_current_tenant_id()) "
        f"WITH CHECK ({check})",
    ]


def disable_tenant_rls(table: str) -> list[str]:
    return [
        f"DROP POLICY IF EXISTS tenant_isolation ON {table}",
        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
    ]


def grant_execute_to_app(signature: str) -> str:
    return f'GRANT EXECUTE ON FUNCTION {signature} TO "{APP_ROLE}"'
