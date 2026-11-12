"""Redis fixed-window rate limiting, used as a FastAPI dependency.

Nginx applies a coarse per-IP limit in production; this adds per-route,
per-identity limits that work across API replicas.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request

from app.core.errors import TooManyRequests
from app.core.redis import get_redis


def client_ip(request: Request) -> str:
    # Nginx sets X-Real-IP; uvicorn runs with --proxy-headers behind it.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


async def hit(key: str, limit: int, window_seconds: int = 60) -> None:
    redis = get_redis()
    bucket = int(time.time() // window_seconds)
    redis_key = f"rl:{key}:{bucket}"
    count = await redis.incr(redis_key)
    if count == 1:
        await redis.expire(redis_key, window_seconds + 1)
    if count > limit:
        retry_after = window_seconds - int(time.time() % window_seconds)
        raise TooManyRequests(retry_after=max(retry_after, 1))


def rate_limit(scope: str, limit_getter: Callable[[], int]) -> Callable[[Request], Awaitable[None]]:
    async def dependency(request: Request) -> None:
        await hit(f"{scope}:{client_ip(request)}", limit_getter())

    return dependency
