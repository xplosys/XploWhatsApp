"""Knowledge base tasks (queue: knowledge)."""

from __future__ import annotations

import uuid

from app.workers.celery_app import celery_app
from app.workers.runtime import run_async


@celery_app.task(name="app.workers.tasks.knowledge.process_version")
def process_version(version_id: str, tenant_id: str) -> str:
    from app.modules.knowledge.service import process_version as run

    return run_async(run(uuid.UUID(version_id), uuid.UUID(tenant_id)))


@celery_app.task(name="app.workers.tasks.knowledge.reindex")
def reindex(tenant_id: str) -> str:
    from app.modules.knowledge.service import reindex as run

    return run_async(run(uuid.UUID(tenant_id)))


@celery_app.task(name="app.workers.tasks.knowledge.sweep")
def sweep() -> dict[str, int]:
    from app.modules.knowledge.sweeper import sweep as run

    return run_async(run())
