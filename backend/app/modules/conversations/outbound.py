"""Outbound messages: policy check, transactional outbox, and the send worker.

Delivery is at-most-once per outbox job. A job is marked `sending` and committed
before Meta is called; if the worker dies during the call, the sweeper marks the
job `unknown` instead of resending, because Meta's send API has no idempotency key
and a duplicate customer message is worse than a visible failure.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, NotFound, ValidationFailed
from app.db.session import app_transaction
from app.modules.conversations import service as conv
from app.modules.conversations.models import (
    Contact,
    Conversation,
    Direction,
    Message,
    MessageStatus,
    SenderType,
)
from app.modules.policy.engine import Decision, MessageKind, evaluate
from app.modules.webhooks.models import OutboxJob, OutboxStatus
from app.modules.whatsapp import service as wa
from app.modules.whatsapp.graph import GraphClient, GraphError
from app.modules.whatsapp.models import (
    AccountHealth,
    AccountStatus,
    CredentialKind,
    MessageTemplate,
    WhatsAppAccount,
    WhatsAppNumber,
)
from app.workers.dispatch import SEND_OUTBOX_JOB, dispatch

log = structlog.get_logger(__name__)

MAX_TEXT_LENGTH = 4096


class PolicyBlocked(AppError):
    status_code = 422
    code = "policy_blocked"
    message = "This message is not allowed right now."


def _blocked(decision: Decision) -> PolicyBlocked:
    return PolicyBlocked(
        decision.message,
        code=decision.code,
        details={"requires_template": decision.requires_template},
    )


@dataclass
class OutboundRequest:
    number_id: uuid.UUID
    contact_id: uuid.UUID | None = None
    phone: str | None = None
    text: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    template_components: list[dict[str, Any]] | None = None
    sender_type: str = SenderType.AGENT
    sent_by_user_id: uuid.UUID | None = None

    @property
    def kind(self) -> MessageKind:
        return MessageKind.TEMPLATE if self.template_name else MessageKind.FREE_FORM


@dataclass
class SendContext:
    account: WhatsAppAccount
    number: WhatsAppNumber
    contact: Contact
    conversation: Conversation | None
    template: MessageTemplate | None


async def _find_template(
    session: AsyncSession, account_id: uuid.UUID, name: str, language: str | None
) -> MessageTemplate | None:
    stmt = select(MessageTemplate).where(
        MessageTemplate.account_id == account_id, MessageTemplate.name == name
    )
    if language:
        stmt = stmt.where(MessageTemplate.language == language)
    stmt = stmt.order_by(MessageTemplate.status != "APPROVED").limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _resolve_contact(session: AsyncSession, tenant_id: uuid.UUID, req: OutboundRequest) -> Contact:
    if req.contact_id:
        contact = await session.get(Contact, req.contact_id)
        if contact is None or contact.tenant_id != tenant_id:
            raise NotFound("Contact not found.")
        return contact
    wa_id = conv.normalise_wa_id(req.phone)
    if not wa_id or not 8 <= len(wa_id) <= 15:
        raise ValidationFailed(
            "Enter the phone number in international format, for example +919876543210.", code="invalid_phone"
        )
    return await conv.upsert_contact(session, tenant_id, wa_id=wa_id)


def build_body(msg: Message, contact: Contact) -> dict[str, Any]:
    """Graph API request body. Phone (to) is preferred; a BSUID (recipient) is used
    for contacts who only shared a username."""
    target: dict[str, Any] = {"to": contact.wa_id} if contact.wa_id else {"recipient": contact.bsuid}
    body: dict[str, Any] = {"recipient_type": "individual", **target}
    if msg.type == "template":
        template = msg.payload.get("template", {})
        body["type"] = "template"
        body["template"] = {
            "name": template["name"],
            "language": {"code": template["language"]},
            **({"components": template["components"]} if template.get("components") else {}),
        }
    else:
        body["type"] = "text"
        body["text"] = {"body": msg.body or "", "preview_url": False}
    return body


async def queue_message(session: AsyncSession, tenant_id: uuid.UUID, req: OutboundRequest) -> Message:
    """Validate, apply policy, and store the message plus its outbox job in the
    caller's transaction. Dispatch happens after commit (see `dispatch_after_commit`)."""
    if req.kind == MessageKind.FREE_FORM:
        if not req.text or not req.text.strip():
            raise ValidationFailed("Message text is empty.", code="empty_message")
        if len(req.text) > MAX_TEXT_LENGTH:
            raise ValidationFailed(
                f"Messages can be at most {MAX_TEXT_LENGTH} characters.", code="message_too_long"
            )

    number = await wa.get_number(session, tenant_id, req.number_id)
    account = await wa.get_account(session, tenant_id, number.account_id)
    contact = await _resolve_contact(session, tenant_id, req)
    conversation = await conv.get_or_create_conversation(session, tenant_id, contact, number.id)
    template = None
    if req.template_name:
        template = await _find_template(session, account.id, req.template_name, req.template_language)
    now = datetime.now(UTC)
    decision = evaluate(
        account=account,
        number=number,
        contact=contact,
        conversation=conversation,
        kind=req.kind,
        now=now,
        template=template,
        template_language=req.template_language,
    )
    if not decision.allowed:
        raise _blocked(decision)

    if template is not None:
        payload: dict[str, Any] = {
            "template": {
                "name": template.name,
                "language": template.language,
                "category": template.category,
                "components": req.template_components or [],
            }
        }
        body_text: str | None = template_preview(template, req.template_components)
    else:
        payload, body_text = {}, req.text
    msg = Message(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        direction=Direction.OUTBOUND,
        sender_type=req.sender_type,
        sent_by_user_id=req.sent_by_user_id,
        type="template" if template else "text",
        body=body_text,
        payload=payload,
        status=MessageStatus.QUEUED,
        template_id=template.id if template else None,
    )
    session.add(msg)
    await session.flush()
    session.add(
        OutboxJob(
            tenant_id=tenant_id,
            kind="send_message",
            aggregate_id=msg.id,
            idempotency_key=f"send_message:{msg.id}",
            status=OutboxStatus.PENDING,
        )
    )
    conversation.last_message_at = now
    await session.flush()
    return msg


def template_preview(template: MessageTemplate, components: list[dict[str, Any]] | None = None) -> str:
    """The template body as the customer sees it, with {{n}} replaced by the body values sent."""
    text = next(
        (
            str(c["text"])
            for c in template.components or []
            if str(c.get("type", "")).upper() == "BODY" and c.get("text")
        ),
        None,
    )
    if text is None:
        return f"Template: {template.name}"
    values: list[str] = []
    for comp in components or []:
        if str(comp.get("type", "")).lower() == "body":
            values = [str(p.get("text", "")) for p in comp.get("parameters", []) if isinstance(p, dict)]
    return re.sub(
        r"\{\{(\d+)\}\}",
        lambda m: values[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(values) else m.group(0),
        text,
    )


async def outbox_job_for(session: AsyncSession, message_id: uuid.UUID) -> OutboxJob | None:
    return (
        await session.execute(select(OutboxJob).where(OutboxJob.aggregate_id == message_id))
    ).scalar_one_or_none()


async def dispatch_job(job: OutboxJob) -> None:
    await dispatch(SEND_OUTBOX_JOB, job.id, job.tenant_id)


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(10 * 2 ** max(attempts - 1, 0), 1800))


@dataclass
class _Prepared:
    token: str
    phone_number_id: str
    body: dict[str, Any]
    account_id: uuid.UUID


async def send_job(job_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """Worker entry point. Returns the job's resulting status."""
    prepared: _Prepared | None = None
    async with app_transaction(tenant_id) as session:
        job = (
            await session.execute(
                select(OutboxJob).where(OutboxJob.id == job_id).with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is None or job.status != OutboxStatus.PENDING:
            return job.status if job else "missing"
        now = datetime.now(UTC)
        if job.next_attempt_at > now:
            return "not_due"
        msg = await session.get(Message, job.aggregate_id)
        if msg is None:
            job.status = OutboxStatus.FAILED
            job.last_error = "message deleted"
            return job.status
        conversation = await session.get(Conversation, msg.conversation_id)
        assert conversation is not None
        contact = await session.get(Contact, conversation.contact_id)
        number = await session.get(WhatsAppNumber, conversation.number_id)
        assert contact is not None and number is not None
        account = await session.get(WhatsAppAccount, number.account_id)
        assert account is not None
        template = await session.get(MessageTemplate, msg.template_id) if msg.template_id else None
        decision = evaluate(
            account=account,
            number=number,
            contact=contact,
            conversation=conversation,
            kind=MessageKind.TEMPLATE if msg.type == "template" else MessageKind.FREE_FORM,
            now=now,
            template=template,
        )
        if not decision.allowed:
            msg.status = MessageStatus.FAILED
            msg.error_code = decision.code
            msg.error_message = decision.message
            msg.status_updated_at = now
            job.status = OutboxStatus.FAILED
            job.last_error = decision.code
            job.finished_at = now
            return job.status
        token = await wa.read_credential(session, account, CredentialKind.BUSINESS_TOKEN)
        if token is None:
            msg.status = MessageStatus.FAILED
            msg.error_code = "account_needs_reconnect"
            msg.error_message = "Reconnect this WhatsApp account."
            job.status = OutboxStatus.FAILED
            job.finished_at = now
            return job.status
        job.status = OutboxStatus.SENDING
        job.attempts += 1
        job.started_at = now
        msg.status = MessageStatus.SENDING
        prepared = _Prepared(token, number.phone_number_id, build_body(msg, contact), account.id)

    assert prepared is not None
    wa_message_id: str | None = None
    error: GraphError | None = None
    try:
        async with GraphClient() as graph:
            wa_message_id = await graph.send_message(prepared.token, prepared.phone_number_id, prepared.body)
    except GraphError as exc:
        error = exc

    async with app_transaction(tenant_id) as session:
        job = await session.get(OutboxJob, job_id, with_for_update=True)
        assert job is not None
        msg = await session.get(Message, job.aggregate_id)
        now = datetime.now(UTC)
        if wa_message_id is not None:
            job.status = OutboxStatus.DONE
            job.finished_at = now
            if msg is not None:
                msg.wa_message_id = wa_message_id
                if msg.status in (MessageStatus.SENDING, MessageStatus.QUEUED):
                    msg.status = MessageStatus.ACCEPTED
                    msg.status_updated_at = now
            return job.status
        assert error is not None
        job.last_error = str(error)[:2000]
        if error.is_token_error:
            account = await session.get(WhatsAppAccount, prepared.account_id)
            if account is not None:
                account.status = AccountStatus.NEEDS_RECONNECT
                account.health = AccountHealth.ERROR
                wa.add_health_issue(
                    account,
                    "token_invalid",
                    "Meta rejected XploWhatsApp's access. Reconnect to resume messaging.",
                )
        if error.retryable and job.attempts < get_settings().outbox_max_attempts:
            job.status = OutboxStatus.PENDING
            job.next_attempt_at = now + _backoff(job.attempts)
            if msg is not None:
                msg.status = MessageStatus.QUEUED
            return "retry"
        job.status = OutboxStatus.FAILED
        job.finished_at = now
        if msg is not None:
            msg.status = MessageStatus.FAILED
            msg.error_code = str(error.code or error.status)[:32]
            msg.error_message = (error.details or error.message)[:500]
            msg.status_updated_at = now
        return job.status


async def mark_stale_sending(job_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """A job stuck in `sending` means the worker died mid-call. Meta may or may not
    have accepted it, so it is surfaced as failed-unknown instead of resent."""
    async with app_transaction(tenant_id) as session:
        job = await session.get(OutboxJob, job_id, with_for_update=True)
        if job is None or job.status != OutboxStatus.SENDING:
            return
        job.status = OutboxStatus.UNKNOWN
        job.finished_at = datetime.now(UTC)
        msg = await session.get(Message, job.aggregate_id)
        if msg is not None and msg.wa_message_id is None:
            msg.status = MessageStatus.FAILED
            msg.error_code = "send_outcome_unknown"
            msg.error_message = "Sending was interrupted. Check WhatsApp before sending again."
