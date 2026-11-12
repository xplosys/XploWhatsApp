"""One-time, idempotent database bootstrap run with a superuser connection.

Creates the four roles, the extensions and default privileges. Everything
else (tables, RLS policies, functions) is created by Alembic as xplo_owner.
"""

from __future__ import annotations

import asyncpg
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.roles import ADMIN_ROLE, APP_ROLE, DEFINER_ROLE, OWNER_ROLE

EXTENSIONS = ("vector", "citext", "pg_trgm")


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _password(url: str) -> str:
    password = make_url(url).password
    if not password:
        raise ValueError("Database URLs must include a password")
    return str(password)


async def bootstrap_database(settings: Settings) -> None:
    if settings.database_superuser_url is None:
        raise ValueError("DATABASE_SUPERUSER_URL is required for bootstrap")
    su_url = make_url(settings.database_superuser_url.get_secret_value()).set(drivername="postgresql")
    db_name = make_url(settings.database_url.get_secret_value()).database
    conn = await asyncpg.connect(su_url.render_as_string(hide_password=False))
    try:
        roles = {
            OWNER_ROLE: ("LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE", settings.database_owner_url),
            APP_ROLE: ("LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB", settings.database_url),
            ADMIN_ROLE: ("LOGIN NOSUPERUSER BYPASSRLS NOCREATEROLE NOCREATEDB", settings.database_admin_url),
        }
        for role, (attrs, url) in roles.items():
            pw = _literal(_password(url.get_secret_value()))
            exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
            verb = "ALTER" if exists else "CREATE"
            await conn.execute(f'{verb} ROLE "{role}" WITH {attrs} PASSWORD {pw}')
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", DEFINER_ROLE):
            await conn.execute(f'CREATE ROLE "{DEFINER_ROLE}" NOLOGIN BYPASSRLS')
        await conn.execute(f'GRANT "{DEFINER_ROLE}" TO "{OWNER_ROLE}"')

        for ext in EXTENSIONS:
            await conn.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')

        await conn.execute(
            f'GRANT CONNECT ON DATABASE "{db_name}" TO "{OWNER_ROLE}", "{APP_ROLE}", "{ADMIN_ROLE}"'
        )
        await conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        await conn.execute(f'GRANT USAGE, CREATE ON SCHEMA public TO "{OWNER_ROLE}", "{DEFINER_ROLE}"')
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{APP_ROLE}", "{ADMIN_ROLE}"')
        for grantee in (APP_ROLE, ADMIN_ROLE):
            await conn.execute(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE "{OWNER_ROLE}" IN SCHEMA public '
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{grantee}"'
            )
            await conn.execute(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE "{OWNER_ROLE}" IN SCHEMA public '
                f'GRANT USAGE, SELECT ON SEQUENCES TO "{grantee}"'
            )
        # Functions are not executable by PUBLIC unless a migration grants them explicitly.
        await conn.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE "{OWNER_ROLE}" REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC'
        )
    finally:
        await conn.close()
