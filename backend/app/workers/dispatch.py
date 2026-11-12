"""Hands work to Celery after the database transaction that created it has committed.

If the broker is unreachable the job is not lost: the row is already stored as
pending, and the beat sweeper re-dispatches it. In tests (TASK_DISPATCH=memory)
jobs are recorded so the test can run the async handler directly.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import structlog

from app.core.config import TaskDispatch, get_settings

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DispatchedJob:
    task: str
    args: tuple[str, ...]


DISPATCHED: list[DispatchedJob] = []


def _send(task: str, *args: str) -> None:
    from app.workers.celery_app import celery_app

    celery_app.send_task(task, args=list(args))


async def dispatch(task: str, *ids: uuid.UUID | str) -> None:
    args = tuple(str(i) for i in ids)
    if get_settings().task_dispatch == TaskDispatch.MEMORY:
        DISPATCHED.append(DispatchedJob(task, args))
        return
    try:
        await asyncio.to_thread(_send, task, *args)
    except Exception:  # broker down: the sweeper picks the job up later
        log.warning("dispatch_failed", task=task, exc_info=True)


PROCESS_WEBHOOK_EVENT = "app.workers.tasks.messages.process_webhook_event"
SEND_OUTBOX_JOB = "app.workers.tasks.messages.send_outbox_job"
