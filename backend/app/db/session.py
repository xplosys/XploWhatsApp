"""Database engines and session helpers.

`xplo_app` is used for all normal traffic and is subject to Row Level Security.
Tenant context is set per transaction with set_config(..., is_local=true), so it
never leaks between pooled connections.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings


def _make_engine(url: str) -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
    )


@lru_cache
def get_app_engine() -> AsyncEngine:
    return _make_engine(get_settings().database_url.get_secret_value())


@lru_cache
def get_admin_engine() -> AsyncEngine:
    return _make_engine(get_settings().database_admin_url.get_secret_value())


@lru_cache
def get_app_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_app_engine(), expire_on_commit=False)


@lru_cache
def get_admin_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_admin_engine(), expire_on_commit=False)


async def set_tenant_context(
    session: AsyncSession, tenant_id: uuid.UUID | None, user_id: uuid.UUID | None = None
) -> None:
    """Set the RLS tenant (and acting user, for auditing) for the current transaction."""
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true), set_config('app.user_id', :uid, true)"),
        {"tid": str(tenant_id) if tenant_id else "", "uid": str(user_id) if user_id else ""},
    )


@asynccontextmanager
async def app_transaction(
    tenant_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None
) -> AsyncIterator[AsyncSession]:
    """One transaction on the RLS-enforced role, optionally bound to a tenant.
    Used by workers and by request dependencies."""
    async with get_app_sessionmaker()() as session, session.begin():
        await set_tenant_context(session, tenant_id, user_id)
        yield session


@asynccontextmanager
async def admin_transaction() -> AsyncIterator[AsyncSession]:
    """Transaction on the BYPASSRLS admin role. Super Admin module only."""
    async with get_admin_sessionmaker()() as session, session.begin():
        yield session


async def dispose_engines() -> None:
    for factory in (get_app_engine, get_admin_engine):
        if factory.cache_info().currsize:
            await factory().dispose()
        factory.cache_clear()
    get_app_sessionmaker.cache_clear()
    get_admin_sessionmaker.cache_clear()


# ---- after-commit hooks ---------------------------------------------------------
# Side effects that must only happen once data is durable (for example handing a
# job to Celery) are registered on the session and run after a successful commit.
# They are dropped on rollback. If one fails, sweepers recover from the stored row.

_AFTER_COMMIT_KEY = "xplo_after_commit"
_background: set[asyncio.Task[None]] = set()


def run_after_commit(session: AsyncSession, fn: Callable[[], Awaitable[None]]) -> None:
    session.sync_session.info.setdefault(_AFTER_COMMIT_KEY, []).append(fn)


@event.listens_for(Session, "after_commit")
def _run_after_commit(sync_session: Session) -> None:
    callbacks = sync_session.info.pop(_AFTER_COMMIT_KEY, [])
    if not callbacks:
        return
    loop = asyncio.get_running_loop()
    for fn in callbacks:
        task = loop.create_task(fn())
        _background.add(task)
        task.add_done_callback(_background.discard)


@event.listens_for(Session, "after_rollback")
def _drop_after_commit(sync_session: Session) -> None:
    sync_session.info.pop(_AFTER_COMMIT_KEY, None)


async def drain_after_commit() -> None:
    """Wait for pending after-commit callbacks (tests and graceful shutdown)."""
    while _background:
        await asyncio.gather(*list(_background), return_exceptions=True)
