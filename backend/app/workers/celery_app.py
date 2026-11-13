from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import worker_process_init

# Workers load models module by module; import the registry so every foreign key resolves.
import app.db.models  # noqa: F401
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
        "app.workers.tasks.whatsapp.*": {"queue": "default"},
        "app.workers.tasks.messages.*": {"queue": "messages"},
        "app.workers.tasks.knowledge.*": {"queue": "knowledge"},
        "app.workers.tasks.billing.*": {"queue": "default"},
        "app.workers.tasks.privacy.*": {"queue": "default"},
    },
    beat_schedule={
        "purge-expired-auth-tokens": {
            "task": "app.workers.tasks.maintenance.purge_expired_auth_tokens",
            "schedule": 6 * 3600.0,
        },
        "sweep-whatsapp-queues": {
            "task": "app.workers.tasks.whatsapp.sweep_queues",
            "schedule": 30.0,
        },
        "sweep-knowledge": {
            "task": "app.workers.tasks.knowledge.sweep",
            "schedule": 60.0,
        },
        "sweep-conversation-summaries": {
            "task": "app.workers.tasks.messages.sweep_summaries",
            "schedule": 120.0,
        },
        "sweep-billing": {
            "task": "app.workers.tasks.billing.sweep",
            "schedule": 600.0,
        },
        "sweep-privacy": {
            "task": "app.workers.tasks.privacy.sweep",
            "schedule": 3600.0,
        },
        "check-whatsapp-accounts": {
            "task": "app.workers.tasks.whatsapp.check_all_accounts",
            "schedule": 24 * 3600.0,
        },
    },
    include=[
        "app.workers.tasks.notifications",
        "app.workers.tasks.maintenance",
        "app.workers.tasks.system",
        "app.workers.tasks.messages",
        "app.workers.tasks.whatsapp",
        "app.workers.tasks.knowledge",
        "app.workers.tasks.billing",
        "app.workers.tasks.privacy",
    ],
)


@worker_process_init.connect
def _init_worker_process(**_: Any) -> None:
    from app.workers.runtime import reset_loop

    reset_loop()
