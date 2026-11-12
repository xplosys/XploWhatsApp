from __future__ import annotations

import asyncio

from tests.conftest import run_alembic


async def test_migrations_roundtrip_and_match_models(database) -> None:  # type: ignore[no-untyped-def]
    from app.cli import _seed_plans
    from app.db.session import dispose_engines

    await dispose_engines()  # release pooled connections before dropping tables
    await asyncio.to_thread(run_alembic, "downgrade", "base")
    await asyncio.to_thread(run_alembic, "upgrade", "head")
    # Fails if a model changed without a migration.
    await asyncio.to_thread(run_alembic, "check")
    await _seed_plans()
