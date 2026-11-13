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
    countdown: float = 0


DISPATCHED: list[DispatchedJob] = []


def _send(task: str, args: tuple[str, ...], countdown: float) -> None:
    from app.workers.celery_app import celery_app

    celery_app.send_task(task, args=list(args), countdown=countdown or None)


async def dispatch(task: str, *ids: uuid.UUID | str, countdown: float = 0) -> None:
    args = tuple(str(i) for i in ids)
    if get_settings().task_dispatch == TaskDispatch.MEMORY:
        DISPATCHED.append(DispatchedJob(task, args, countdown))
        return
    try:
        await asyncio.to_thread(_send, task, args, countdown)
    except Exception:  # broker down: the sweeper picks the job up later
        log.warning("dispatch_failed", task=task, exc_info=True)


PROCESS_WEBHOOK_EVENT = "app.workers.tasks.messages.process_webhook_event"
SEND_OUTBOX_JOB = "app.workers.tasks.messages.send_outbox_job"
AI_AUTO_REPLY = "app.workers.tasks.messages.ai_auto_reply"
CONVERSATION_SUMMARY = "app.workers.tasks.messages.summarise_conversation"
KNOWLEDGE_PROCESS = "app.workers.tasks.knowledge.process_version"
KNOWLEDGE_REINDEX = "app.workers.tasks.knowledge.reindex"
BILLING_EVENT = "app.workers.tasks.billing.process_event"
DATA_EXPORT = "app.workers.tasks.privacy.build_export"
