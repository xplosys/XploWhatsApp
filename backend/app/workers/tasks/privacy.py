"""Privacy tasks (queue: default): build data exports, and an hourly sweeper for
expired exports, purging deleted businesses, message retention and old webhook payloads."""

from __future__ import annotations

import uuid
from typing import Any

from app.workers.celery_app import celery_app
from app.workers.runtime import run_async


@celery_app.task(name="app.workers.tasks.privacy.build_export")
def build_export(export_id: str, tenant_id: str) -> str:
    from app.modules.privacy.service import build_export as run

    return run_async(run(uuid.UUID(export_id), uuid.UUID(tenant_id)))


@celery_app.task(name="app.workers.tasks.privacy.sweep")
def sweep() -> dict[str, Any]:
    from app.modules.privacy.sweeper import sweep as run

    return run_async(run())
