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

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.redis import get_redis
from app.db.session import app_transaction, run_after_commit
from app.modules.ai import catalogue, orchestrator
from app.modules.ai.assistant import AssistantConfig, active_config, get_assistant
from app.modules.ai.catalogue import CallRecord
from app.modules.ai.orchestrator import ChainStep, Outcome
from app.modules.ai.prompt import Passage, fit_passages, trim_history
from app.modules.ai.providers.base import Turn
from app.modules.appointments import service as appointments
from app.modules.billing.service import effective_plan
from app.modules.conversations import inbox, outbound
from app.modules.conversations.models import (
    ConsentStatus,
    Contact,
    Conversation,
    Direction,
    HandoverSession,
    HandoverStarter,
    Message,
    SenderType,
)
from app.modules.knowledge import retrieval
from app.modules.policy.engine import service_window_open
from app.modules.tenants.models import Tenant
from app.workers.dispatch import AI_AUTO_REPLY, CONVERSATION_SUMMARY, dispatch

log = structlog.get_logger(__name__)

LOCK_TTL_SECONDS = 180
# How far back "already asked for feedback" and "already greeted" look.
CONVERSATION_MEMORY = timedelta(hours=24)
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
    """The recent messages the assistant answers from. When the team handed the
    conversation back, it starts there: what came before was dealt with by a person,
    and an old "let me speak to someone" would otherwise hand it over again."""
    settings = get_settings()
    handed_back = await session.scalar(
        select(func.max(HandoverSession.released_at)).where(
            HandoverSession.conversation_id == conversation_id
        )
    )
    query = select(Message).where(Message.conversation_id == conversation_id)
    if handed_back is not None:
        query = query.where(Message.created_at > handed_back)
    rows = list(
        (
            await session.execute(
                query.order_by(Message.created_at.desc()).limit(settings.ai_history_messages)
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


@dataclass(frozen=True)
class Manners:
    """What the assistant needs to know to greet once and ask for feedback once."""

    feedback_asked: bool
    first_reply: bool


async def _manners(session: AsyncSession, conversation_id: uuid.UUID) -> Manners:
    """Feedback counts as asked when the assistant wrapped up in the last day (since the
    team last handed the conversation back). A reply is the first one when nothing was
    sent in the last day, or since the assistant last said goodbye (a wrap-up invites an
    answer, so the reply to it is not a new conversation)."""
    since = datetime.now(UTC) - CONVERSATION_MEMORY
    ai_in_conversation = (
        Message.conversation_id == conversation_id,
        Message.sender_type == SenderType.AI,
        Message.created_at > since,
    )
    handed_back = await session.scalar(
        select(func.max(HandoverSession.released_at)).where(
            HandoverSession.conversation_id == conversation_id
        )
    )
    asked = await session.scalar(
        select(Message.id)
        .where(
            *ai_in_conversation,
            Message.ai_provenance["closing"].astext == "wrap_up",
            *((Message.created_at > handed_back,) if handed_back is not None else ()),
        )
        .limit(1)
    )
    last_goodbye = await session.scalar(
        select(func.max(Message.created_at)).where(
            *ai_in_conversation, Message.ai_provenance["closing"].astext == "goodbye"
        )
    )
    start = max(since, last_goodbye) if last_goodbye is not None else since
    sent = await session.scalar(
        select(Message.id)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == Direction.OUTBOUND,
            Message.created_at > start,
        )
        .limit(1)
    )
    return Manners(feedback_asked=asked is not None, first_reply=sent is None)


@dataclass
class Prepared:
    config: AssistantConfig
    steps: list[ChainStep]
    turns: list[Turn]
    business_name: str
    default_language: str
    number_id: uuid.UUID
    contact_id: uuid.UUID
    retrieval: retrieval.RetrievalPlan | str = "not_configured"
    extra_calls: list[CallRecord] = field(default_factory=list)
    booking: appointments.BookingContext | None = None
    manners: Manners = Manners(feedback_asked=False, first_reply=False)
    customer_name: str | None = None


async def _hand_over(
    session: AsyncSession,
    conversation: Conversation,
    *,
    trigger_id: uuid.UUID,
    reason: str,
    prepared: Prepared,
    retrieval_hits: int | None,
) -> None:
    """Stop automatic replies, keep a record of the question the assistant could not
    answer, and (if the business wants it) write a summary for whoever picks it up."""
    await inbox.start_handover(session, conversation, started_by=HandoverStarter.AI, reason=reason)
    trigger = await session.get(Message, trigger_id)
    question = (trigger.body or "").strip() if trigger is not None else ""
    if not question:
        question = next((t.text for t in reversed(prepared.turns) if t.role == "user"), "")
    if question:
        await inbox.record_unanswered(
            session,
            conversation,
            message_id=trigger_id,
            question=question,
            reason=reason,
            retrieval_hits=retrieval_hits,
        )
    if prepared.config.summary_on_handover:
        await inbox.request_summary(session, conversation, None)
        conversation_id, tenant_id = conversation.id, conversation.tenant_id
        run_after_commit(session, lambda: dispatch(CONVERSATION_SUMMARY, conversation_id, tenant_id))


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
        retrieval=await retrieval.plan_for(session, tenant_id),
        booking=await appointments.ai_context(session, tenant, conversation.contact_id),
        manners=await _manners(session, conversation_id),
        customer_name=contact.name or contact.profile_name,
    )


async def _retrieve(
    tenant_id: uuid.UUID, conversation_id: uuid.UUID, prepared: Prepared
) -> tuple[list[Passage], dict[str, object]]:
    """Knowledge passages for the customer's latest messages. The query is embedded
    with no transaction open; only the vector search touches the database."""
    plan = prepared.retrieval
    if not isinstance(plan, retrieval.RetrievalPlan):
        return [], {"hits": 0, "status": plan}
    vectors, call, error = await retrieval.embed_queries(
        plan, retrieval.queries_from_turns(prepared.turns), purpose="retrieval"
    )
    if call is not None:
        prepared.extra_calls.append(dataclasses.replace(call, conversation_id=conversation_id))
    if vectors is None:
        return [], {"hits": 0, "error": error}
    async with app_transaction(tenant_id) as session:
        passages = await retrieval.nearest_any(
            session, tenant_id, plan.embedder.model_pk, vectors, top_k=plan.top_k, min_score=plan.min_score
        )
    passages = fit_passages(passages)
    return passages, {"hits": len(passages)}


async def _deliver(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    trigger_id: uuid.UUID,
    prepared: Prepared,
    outcome: Outcome,
) -> str:
    for call in prepared.extra_calls:
        await catalogue.record_call(session, tenant_id, call)
    for call in orchestrator.call_records(outcome, purpose="auto_reply", conversation_id=conversation_id):
        await catalogue.record_call(session, tenant_id, call)
    conversation = await session.get(Conversation, conversation_id, with_for_update=True)
    if conversation is None:
        return "missing"
    if await _latest_inbound_id(session, conversation_id) != trigger_id:
        return "superseded"  # a newer message arrived while the model was thinking
    if not conversation.ai_enabled:
        return "ai_off"  # an agent took over meanwhile
    closing = _closing(outcome, prepared, conversation)
    provenance: dict[str, object] = {**outcome.provenance(), "reply_to": str(trigger_id)}
    provenance.pop("closing", None)
    if closing != "none":
        provenance["closing"] = closing
    text = outcome.text
    # Booking and the message that reports it stand or fall together: if the message
    # cannot be queued, the appointment is not kept either.
    savepoint = None
    if outcome.kind == "reply" and outcome.booking is not None and prepared.booking is not None:
        savepoint = await session.begin_nested()
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        try:
            action = await appointments.apply_ai_action(
                session, tenant, conversation, prepared.booking, outcome.booking, reply=outcome.text
            )
        except AppError as exc:  # e.g. the doctor was removed while the model was answering
            await savepoint.rollback()
            savepoint = await session.begin_nested()
            action = appointments.ActionResult(
                False,
                "Sorry, I couldn't complete that just now. "
                "Please try again, or ask for a member of our team.",
                {"action": outcome.booking.action, "result": exc.code},
            )
        text = action.text
        provenance["booking"] = action.details
    try:
        msg = await outbound.queue_message(
            session,
            tenant_id,
            outbound.OutboundRequest(
                number_id=prepared.number_id,
                contact_id=prepared.contact_id,
                text=text,
                sender_type=SenderType.AI,
                ai_provenance=provenance,
            ),
        )
    except AppError as exc:
        if savepoint is not None:
            await savepoint.rollback()
        log.warning("ai_reply_blocked", conversation_id=str(conversation_id), code=exc.code)
        if exc.code in ACCOUNT_BLOCKS:
            # Nobody could send from this number right now, a person included. Turning
            # the assistant off would leave the customer unanswered after the account is
            # reconnected, so the conversation stays with the assistant. The account's
            # health alert tells the business what to fix.
            return f"blocked:{exc.code}"
        await _hand_over(
            session,
            conversation,
            trigger_id=trigger_id,
            reason=f"blocked:{exc.code}",
            prepared=prepared,
            retrieval_hits=_hits(outcome),
        )
        return f"blocked:{exc.code}"
    if savepoint is not None:
        await savepoint.commit()
    await catalogue.record_reply(session, tenant_id, conversation_id)
    if closing != "none":
        # The query is answered: the conversation leaves the open inbox. If the customer
        # writes again (more questions, or their feedback), it reopens by itself.
        inbox.set_status(conversation, "closed")
    if outcome.kind == "handover":
        await _hand_over(
            session,
            conversation,
            trigger_id=trigger_id,
            reason=outcome.reason,
            prepared=prepared,
            retrieval_hits=_hits(outcome),
        )
    job = await outbound.outbox_job_for(session, msg.id)
    if job is not None:
        run_after_commit(session, lambda: outbound.dispatch_job(job))
    if outcome.kind == "handover":
        return "handover"
    booking = provenance.get("booking")
    if isinstance(booking, dict):
        return f"booking:{booking['result']}"
    return f"closed:{closing}" if closing != "none" else "replied"


def _closing(outcome: Outcome, prepared: Prepared, conversation: Conversation) -> str:
    """Whether this reply ends the conversation. Never for a handover, a booking action,
    or a conversation waiting for a person; and feedback is asked for at most once."""
    if (
        outcome.kind != "reply"
        or outcome.closing == "none"
        or outcome.booking is not None
        or outcome.out_of_scope
        or conversation.needs_human
        or not prepared.config.closing_enabled
    ):
        return "none"
    if outcome.closing == "wrap_up" and prepared.manners.feedback_asked:
        return "goodbye"
    return outcome.closing


# Policy denials about the WhatsApp account or number rather than the conversation.
ACCOUNT_BLOCKS = frozenset({"account_needs_reconnect", "account_not_connected", "number_not_connected"})


def _hits(outcome: Outcome) -> int | None:
    hits = (outcome.retrieval or {}).get("hits")
    return hits if isinstance(hits, int) else None


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
        knowledge, retrieval_info = await _retrieve(tenant_id, conversation_id, prepared)
        outcome = await orchestrator.run(
            prepared.steps,
            prepared.config,
            business_name=prepared.business_name,
            default_language=prepared.default_language,
            turns=prepared.turns,
            knowledge=knowledge,
            retrieval=retrieval_info,
            booking=prepared.booking,
            feedback_asked=prepared.manners.feedback_asked,
            greet_name=prepared.customer_name if prepared.manners.first_reply else None,
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
