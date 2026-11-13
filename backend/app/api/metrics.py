"""GET /metrics for Prometheus. Not routed by Nginx; scraped inside the private network
with a bearer token (METRICS_TOKEN). Without a token it answers only in development."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request, Response
from redis.asyncio import Redis

from app.core import metrics
from app.core.config import get_settings
from app.core.errors import NotFound

router = APIRouter(include_in_schema=False)


async def _update_queue_depth() -> None:
    """Celery's Redis broker keeps each queue as a list named after it."""
    redis = Redis.from_url(get_settings().broker_url, socket_timeout=2)
    try:
        for queue in metrics.QUEUES:
            try:
                metrics.QUEUE_DEPTH.labels(queue).set(await redis.llen(queue))
            except Exception:  # the scrape still reports everything else
                metrics.QUEUE_DEPTH.labels(queue).set(float("nan"))
    finally:
        await redis.aclose()


@router.get("/metrics")
async def prometheus_metrics(request: Request) -> Response:
    settings = get_settings()
    token = settings.metrics_token.get_secret_value() if settings.metrics_token else None
    if token:
        sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(sent.encode(), token.encode()):
            raise NotFound()
    elif settings.is_production_like:
        raise NotFound()
    await _update_queue_depth()
    body, content_type = metrics.render()
    return Response(body, media_type=content_type)
