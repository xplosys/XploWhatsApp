"""Prometheus metrics for the API and the workers.

Labels are always from small fixed sets (route templates, task names, short result
words), never ids, phone numbers or tenant names, so the series count stays bounded.

When PROMETHEUS_MULTIPROC_DIR is set (several uvicorn workers, or Celery's prefork
children), every process writes its samples to that directory and the exporter adds
them up. It must be an empty directory at start: the containers use a tmpfs.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client import REGISTRY as DEFAULT_REGISTRY
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MULTIPROC = bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))

HTTP_REQUESTS = Counter(
    "xplo_http_requests_total",
    "API requests by route template, method and status code.",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "xplo_http_request_duration_seconds",
    "API request duration by route template.",
    ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
TASKS = Counter(
    "xplo_task_runs_total",
    "Background tasks by name and result. The result is the task's own outcome word "
    "(for example replied, handover, done, failed) or error when it raised.",
    ["task", "result"],
)
TASK_LATENCY = Histogram(
    "xplo_task_duration_seconds",
    "Background task duration by name.",
    ["task"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
AI_ATTEMPTS = Counter(
    "xplo_ai_attempts_total",
    "Calls to AI providers by provider and result (ok, invalid, skipped or the error code).",
    ["provider", "result"],
)
AI_LATENCY = Histogram(
    "xplo_ai_attempt_duration_seconds",
    "AI provider call duration.",
    ["provider"],
    buckets=(0.25, 0.5, 1, 2, 4, 8, 15, 30, 60),
)
QUEUE_DEPTH = Gauge(
    "xplo_queue_depth",
    "Tasks waiting in each Celery queue, read when metrics are scraped.",
    ["queue"],
    multiprocess_mode="mostrecent",
)

QUEUES = ("default", "messages", "knowledge")
_WORD = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def result_label(value: object) -> str:
    """A task's return value as a label: short lowercase words pass, anything else is
    'ok' (counts, dicts, None), so no id can become a label."""
    if isinstance(value, str) and _WORD.match(value):
        return value
    return "ok"


def short_task_name(name: str | None) -> str:
    return (name or "unknown").removeprefix("app.workers.tasks.")


def observe_task(name: str | None, seconds: float | None, result: str) -> None:
    task = short_task_name(name)
    TASKS.labels(task, result).inc()
    if seconds is not None:
        TASK_LATENCY.labels(task).observe(seconds)


def observe_ai(attempts: Iterable[object]) -> None:
    """Every provider call an answer took, including previews and fallbacks. What
    happened to the customer's message is counted by the ai_auto_reply task result."""
    for a in attempts:
        provider = str(getattr(a, "provider", "unknown"))[:20]
        result = getattr(a, "outcome", "unknown")
        AI_ATTEMPTS.labels(
            provider, result if result in ("ok", "invalid", "skipped") else result_label(result)
        ).inc()
        latency = getattr(a, "latency_ms", 0)
        if latency:
            AI_LATENCY.labels(provider).observe(latency / 1000)


def registry() -> CollectorRegistry:
    if MULTIPROC:
        reg = CollectorRegistry()
        multiprocess.MultiProcessCollector(reg)  # type: ignore[no-untyped-call]
        return reg
    return DEFAULT_REGISTRY


def render() -> tuple[bytes, str]:
    return generate_latest(registry()), CONTENT_TYPE_LATEST


def route_template(scope: Scope) -> str:
    """The matched route with its router prefixes, e.g. /api/v1/leads/{lead_id}.
    Included routers keep only their own part of the path, so the static prefix is
    taken from the real path: as many leading segments as the route doesn't cover."""
    route = scope.get("route")
    own = getattr(route, "path", None)
    if not own:
        return "unmatched"
    path_parts = [p for p in scope.get("path", "").split("/") if p]
    own_parts = [p for p in own.split("/") if p]
    prefix = path_parts[: max(0, len(path_parts) - len(own_parts))]
    return "/" + "/".join([*prefix, *own_parts]) if prefix else own


class MetricsMiddleware:
    """Counts and times every request by its route template (/api/v1/leads/{lead_id},
    not the real id). Paths that match no route are counted as 'unmatched'."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/metrics":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            template = route_template(scope)
            method = scope.get("method", "GET")
            HTTP_REQUESTS.labels(method, template, str(status)).inc()
            HTTP_LATENCY.labels(method, template).observe(time.perf_counter() - start)
