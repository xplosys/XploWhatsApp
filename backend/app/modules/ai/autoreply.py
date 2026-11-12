"""Automatic replies to inbound WhatsApp messages.

1. After an inbound message is stored, a reply job is scheduled a couple of seconds
   later (`ai_reply_debounce_seconds`), so a burst of messages gets one answer.
2. The job takes a per-conversation lock, checks it is still the newest message and
   that the assistant may answer, and builds the context (short transaction).
3. The providers run with no transaction open.
4. A second transaction re-checks the conversation, records usage, and queues the
   reply through the normal outbox and policy engine. A handover sends the
   business's handover message and turns AI off for that conversation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.redis import get_redis
from app.db.session import app_transaction, run_after_commit
from app.modules.ai import catalogue, orchestrator
from app.modules.ai.assistant import AssistantConfig, active_config, get_assistant
from app.modules.ai.orchestrator import ChainStep, Outcome
from app.modules.ai.prompt import trim_history
from app.modules.ai.providers.base import Turn
from app.modules.billing.service import effective_plan
from app.modules.conversations import outbound
from app.modules.conversations.models import (
    ConsentStatus,
    Contact,
    Conversation,
    Direction,
    Message,
    SenderType,
)
from app.modules.policy.engine import service_window_open
from app.modules.tenants.models import Tenant
from app.workers.dispatch import AI_AUTO_REPLY, dispatch

log = structlog.get_logger(__name__)

LOCK_TTL_SECONDS = 180
BUSY_RETRY_SECONDS = 5.0

MEDIA_LABELS = {
    "image": "an image",
    "audio": "a voice message",
    "video": "a video",
    "document": "a document",
    "sticker": "a sticker",
    "location": "a location",
    "contacts": "a contact card",
}


async def schedule_after_inbound(session: AsyncSession, tenant_id: uuid.UUID, message: Message) -> None:
    """Called in the webhook transaction for each new inbound message."""
    conversation = await session.get(Conversation, message.conversation_id)
    if conversation is None or not conversation.ai_enabled:
        return
    assistant = await get_assistant(session, tenant_id)
    if assistant is None or not assistant.auto_reply_enabled or assistant.active_version is None:
        return
    delay = get_settings().ai_reply_debounce_seconds
    run_after_commit(
        session,
        lambda: dispatch(AI_AUTO_REPLY, conversation.id, tenant_id, message.id, countdown=delay),
    )


def _turn(message: Message) -> Turn | None:
    if message.direction == Direction.INBOUND:
        if message.type in ("text", "button", "interactive") and message.body:
            return Turn("user", message.body)
        if message.type == "reaction":
            return None
        label = MEDIA_LABELS.get(message.type, "a message the assistant cannot read")
        caption = f' with the caption "{message.body}"' if message.body else ""
        return Turn("user", f"[The customer sent {label}{caption}.]")
    if message.body and message.status != "failed":
        return Turn("assistant", message.body)
    return None


async def history(session: AsyncSession, conversation_id: uuid.UUID) -> list[Turn]:
    settings = get_settings()
    rows = list(
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc())
                .limit(settings.ai_history_messages)
            )
        ).scalars()
    )
    turns = [t for t in (_turn(m) for m in reversed(rows)) if t is not None]
    return trim_history(turns, settings.ai_history_max_chars)


async def _latest_inbound_id(session: AsyncSession, conversation_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        select(Message.id)
        .where(Message.conversation_id == conversation_id, Message.direction == Direction.INBOUND)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )


async def _already_answered(session: AsyncSession, conversation_id: uuid.UUID, trigger_id: uuid.UUID) -> bool:
    return (
        await session.scalar(
            select(Message.id)
            .where(
                Message.conversation_id == conversation_id,
                Message.sender_type == SenderType.AI,
                Message.ai_provenance["reply_to"].astext == str(trigger_id),
            )
            .limit(1)
        )
    ) is not None


@dataclass
class Prepared:
    config: AssistantConfig
    steps: list[ChainStep]
    turns: list[Turn]
    business_name: str
    default_language: str
    number_id: uuid.UUID
    contact_id: uuid.UUID


def _hand_over(conversation: Conversation) -> None:
    conversation.ai_enabled = False
    conversation.needs_human = True


async def _prepare(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID, trigger_id: uuid.UUID
) -> Prepared | str:
    conversation = await session.get(Conversation, conversation_id, with_for_update=True)
    if conversation is None:
        return "missing"
    if await _latest_inbound_id(session, conversation_id) != trigger_id:
        return "superseded"
    if await _already_answered(session, conversation_id, trigger_id):
        return "duplicate"
    if not conversation.ai_enabled:
        return "ai_off"
    assistant = await get_assistant(session, tenant_id)
    if assistant is None or not assistant.auto_reply_enabled:
        return "assistant_off"
    config = await active_config(session, assistant)
    if config is None:
        return "assistant_off"
    contact = await session.get(Contact, conversation.contact_id)
    if contact is None or contact.consent_status == ConsentStatus.OPTED_OUT:
        return "opted_out"
    if not service_window_open(conversation, datetime.now(UTC)):
        return "window_closed"
    plan = await effective_plan(session, tenant_id)
    limit = plan.limit("monthly_ai_messages", 0)
    if await catalogue.replies_this_month(session, tenant_id) >= limit:
        # No AI reply and no automatic message: a person answers from the inbox.
        conversation.needs_human = True
        log.info("ai_quota_reached", tenant_id=str(tenant_id), limit=limit)
        return "quota_exceeded"
    turns = await history(session, conversation_id)
    if not turns:
        return "nothing_to_answer"
    tenant = await session.get(Tenant, tenant_id)
    assert tenant is not None
    return Prepared(
        config=config,
        steps=await orchestrator.prepare_chain(session, tenant_id, config),
        turns=turns,
        business_name=tenant.name,
        default_language=tenant.default_language or "en",
        number_id=conversation.number_id,
        contact_id=conversation.contact_id,
    )


async def _deliver(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    trigger_id: uuid.UUID,
    prepared: Prepared,
    outcome: Outcome,
) -> str:
    for call in orchestrator.call_records(outcome, purpose="auto_reply", conversation_id=conversation_id):
        await catalogue.record_call(session, tenant_id, call)
    conversation = await session.get(Conversation, conversation_id, with_for_update=True)
    if conversation is None:
        return "missing"
    if await _latest_inbound_id(session, conversation_id) != trigger_id:
        return "superseded"  # a newer message arrived while the model was thinking
    if not conversation.ai_enabled:
        return "ai_off"  # an agent took over meanwhile
    provenance = {**outcome.provenance(), "reply_to": str(trigger_id)}
    try:
        msg = await outbound.queue_message(
            session,
            tenant_id,
            outbound.OutboundRequest(
                number_id=prepared.number_id,
                contact_id=prepared.contact_id,
                text=outcome.text,
                sender_type=SenderType.AI,
                ai_provenance=provenance,
            ),
        )
    except AppError as exc:
        log.warning("ai_reply_blocked", conversation_id=str(conversation_id), code=exc.code)
        _hand_over(conversation)
        return f"blocked:{exc.code}"
    await catalogue.record_reply(session, tenant_id, conversation_id)
    if outcome.kind == "handover":
        _hand_over(conversation)
    job = await outbound.outbox_job_for(session, msg.id)
    if job is not None:
        run_after_commit(session, lambda: outbound.dispatch_job(job))
    return "handover" if outcome.kind == "handover" else "replied"


async def reply(conversation_id: uuid.UUID, tenant_id: uuid.UUID, trigger_id: uuid.UUID) -> str:
    redis = get_redis()
    lock_key = f"ai_reply_lock:{conversation_id}"
    token = uuid.uuid4().hex
    if not await redis.set(lock_key, token, nx=True, ex=LOCK_TTL_SECONDS):
        # Another reply for this conversation is running; look again shortly.
        await dispatch(AI_AUTO_REPLY, conversation_id, tenant_id, trigger_id, countdown=BUSY_RETRY_SECONDS)
        return "busy"
    try:
        async with app_transaction(tenant_id) as session:
            prepared = await _prepare(session, tenant_id, conversation_id, trigger_id)
        if isinstance(prepared, str):
            return prepared
        outcome = await orchestrator.run(
            prepared.steps,
            prepared.config,
            business_name=prepared.business_name,
            default_language=prepared.default_language,
            turns=prepared.turns,
        )
        async with app_transaction(tenant_id) as session:
            result = await _deliver(session, tenant_id, conversation_id, trigger_id, prepared, outcome)
        log.info(
            "ai_auto_reply",
            conversation_id=str(conversation_id),
            result=result,
            reason=outcome.reason,
            provider=outcome.provider,
            model=outcome.model,
        )
        return result
    finally:
        if await redis.get(lock_key) == token:
            await redis.delete(lock_key)
