"""Async runtime for Celery workers.

Celery tasks are synchronous. Each worker process keeps one long-lived asyncio
event loop, so async SQLAlchemy engines and HTTP clients are reused across tasks
instead of being rebuilt per task.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
        return _loop


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    return _get_loop().run_until_complete(coro)


def reset_loop() -> None:
    """Called after fork so each child process gets its own loop and engines."""
    global _loop
    from app.core.redis import get_redis
    from app.db.session import get_admin_engine, get_admin_sessionmaker, get_app_engine, get_app_sessionmaker

    for cached in (get_app_engine, get_admin_engine, get_app_sessionmaker, get_admin_sessionmaker, get_redis):
        cached.cache_clear()
    _loop = None
