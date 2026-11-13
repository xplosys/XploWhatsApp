"""Billing tasks (queue: default): apply gateway webhooks, and a sweeper for trials,
period-end changes and lost webhook jobs."""

from __future__ import annotations

import uuid
from typing import Any

from app.workers.celery_app import celery_app
from app.workers.runtime import run_async


@celery_app.task(name="app.workers.tasks.billing.process_event")
def process_event(event_id: str, tenant_id: str) -> str:
    from app.modules.billing.events import process

    return run_async(process(uuid.UUID(event_id), uuid.UUID(tenant_id)))


@celery_app.task(name="app.workers.tasks.billing.sweep")
def sweep() -> dict[str, Any]:
    from app.modules.billing.notices import sweep as run

    return dict(run_async(run()))
