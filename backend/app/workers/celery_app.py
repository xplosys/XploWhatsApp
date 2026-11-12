from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import worker_process_init

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level, settings.log_json)

celery_app = Celery("xplowhatsapp", broker=settings.broker_url, backend=settings.result_backend)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    result_expires=3600,
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_default_queue="default",
    # Queues for later phases are declared now so deployments do not change shape.
    task_routes={
        "app.workers.tasks.notifications.*": {"queue": "default"},
        "app.workers.tasks.maintenance.*": {"queue": "default"},
        "app.workers.tasks.messages.*": {"queue": "messages"},
        "app.workers.tasks.knowledge.*": {"queue": "knowledge"},
    },
    beat_schedule={
        "purge-expired-auth-tokens": {
            "task": "app.workers.tasks.maintenance.purge_expired_auth_tokens",
            "schedule": 6 * 3600.0,
        },
    },
    include=["app.workers.tasks.notifications", "app.workers.tasks.maintenance", "app.workers.tasks.system"],
)


@worker_process_init.connect
def _init_worker_process(**_: Any) -> None:
    from app.workers.runtime import reset_loop

    reset_loop()
