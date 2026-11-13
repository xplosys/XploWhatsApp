"""Redis fixed-window rate limiting, used as a FastAPI dependency.

Nginx applies a coarse per-IP limit in production; this adds per-route,
per-identity limits that work across API replicas.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache

from fastapi import Request

from app.core.config import get_settings
from app.core.errors import TooManyRequests
from app.core.redis import get_redis


@lru_cache(maxsize=8)
def _trusted_networks(cidrs: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(ipaddress.ip_network(c, strict=False) for c in cidrs)


def _is_trusted_proxy(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _trusted_networks(tuple(get_settings().trusted_proxies)))


def client_ip(request: Request) -> str:
    """The caller's address. Nginx sets X-Real-IP, but the header is only believed when
    the connection comes from a trusted proxy; otherwise anyone could choose an address
    for each request and step around per-address limits."""
    peer = request.client.host if request.client else None
    forwarded = request.headers.get("x-real-ip", "").strip()
    if forwarded and (peer is None or _is_trusted_proxy(peer)):
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer or "unknown"


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
