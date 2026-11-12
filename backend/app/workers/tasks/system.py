from __future__ import annotations

from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.tasks.system.ping")
def ping() -> str:
    return "pong"
