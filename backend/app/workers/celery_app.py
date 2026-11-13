from __future__ import annotations

import os
import time
from typing import Any

from celery import Celery
from celery.signals import (
    task_postrun,
    task_prerun,
    worker_init,
    worker_process_init,
    worker_process_shutdown,
)

# Workers load models module by module; import the registry so every foreign key resolves.
import app.db.models  # noqa: F401
from app.core import metrics
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.observability import init_sentry

settings = get_settings()
configure_logging(settings.log_level, settings.log_json)
init_sentry(settings, "worker")

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


# ---- metrics (app/core/metrics.py) ------------------------------------------------------

_started: dict[str, float] = {}


@worker_init.connect
def _start_metrics_server(**_: Any) -> None:
    """Main worker process: clear samples left by a previous run, then serve /metrics for
    every child process on WORKER_METRICS_PORT (private network only)."""
    from prometheus_client import start_http_server

    directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if directory:
        for name in os.listdir(directory):
            if name.endswith(".db"):
                os.remove(os.path.join(directory, name))
    start_http_server(settings.worker_metrics_port, registry=metrics.registry())


@worker_process_shutdown.connect
def _mark_process_dead(pid: int | None = None, **_: Any) -> None:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(pid or os.getpid())  # type: ignore[no-untyped-call]


@task_prerun.connect
def _task_started(task_id: str | None = None, **_: Any) -> None:
    if task_id:
        _started[task_id] = time.perf_counter()


@task_postrun.connect
def _task_finished(
    task_id: str | None = None, task: Any = None, retval: Any = None, state: str | None = None, **_: Any
) -> None:
    started = _started.pop(task_id, None) if task_id else None
    seconds = time.perf_counter() - started if started is not None else None
    if state == "FAILURE":
        result = "error"
    elif state == "RETRY":
        result = "retry"
    else:
        result = metrics.result_label(retval)
    metrics.observe_task(getattr(task, "name", None), seconds, result)
