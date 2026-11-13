"""WhatsApp message processing tasks (queue: messages)."""

from __future__ import annotations

import uuid

import structlog

from app.workers.celery_app import celery_app
from app.workers.runtime import run_async

log = structlog.get_logger(__name__)


@celery_app.task(name="app.workers.tasks.messages.process_webhook_event")
def process_webhook_event(event_id: str, tenant_id: str) -> str:
    from app.modules.webhooks.processor import process_event

    return run_async(process_event(uuid.UUID(event_id), uuid.UUID(tenant_id)))


@celery_app.task(name="app.workers.tasks.messages.send_outbox_job")
def send_outbox_job(job_id: str, tenant_id: str) -> str:
    from app.modules.conversations.outbound import send_job

    return run_async(send_job(uuid.UUID(job_id), uuid.UUID(tenant_id)))


@celery_app.task(name="app.workers.tasks.messages.ai_auto_reply")
def ai_auto_reply(conversation_id: str, tenant_id: str, trigger_message_id: str) -> str:
    from app.modules.ai.autoreply import reply

    return run_async(reply(uuid.UUID(conversation_id), uuid.UUID(tenant_id), uuid.UUID(trigger_message_id)))


@celery_app.task(name="app.workers.tasks.messages.summarise_conversation")
def summarise_conversation(conversation_id: str, tenant_id: str) -> str:
    from app.modules.conversations.summary import summarise

    return run_async(summarise(uuid.UUID(conversation_id), uuid.UUID(tenant_id)))


@celery_app.task(name="app.workers.tasks.messages.sweep_summaries")
def sweep_summaries() -> int:
    from app.modules.conversations.summary import sweep

    return run_async(sweep())
