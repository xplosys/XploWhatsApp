"""WhatsApp messages about appointments: telling a customer about a change made by
staff, and reminders before the appointment.

Inside the 24-hour customer service window a plain text message is sent. Outside it,
WhatsApp only allows an approved template, so reminders use the template the business
chose. Its body variables are filled in this order (a template may use the first few):
{{1}} patient name, {{2}} doctor or service, {{3}} date, {{4}} time, {{5}} reference,
{{6}} business name.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.session import app_transaction, run_after_commit
from app.modules.appointments import slots
from app.modules.appointments.models import Appointment, AppointmentStatus, BookableResource
from app.modules.appointments.service import get_settings_row, resource_label
from app.modules.conversations import outbound
from app.modules.conversations.models import Conversation, SenderType
from app.modules.policy.engine import service_window_open
from app.modules.tenants.models import Tenant
from app.modules.whatsapp.models import MessageTemplate, NumberStatus, WhatsAppNumber

log = structlog.get_logger(__name__)

TEMPLATE_VARIABLES = 6
_VAR = re.compile(r"\{\{(\d+)\}\}")


async def _conversation(session: AsyncSession, appt: Appointment) -> Conversation | None:
    if appt.conversation_id:
        conv = await session.get(Conversation, appt.conversation_id)
        if conv is not None and conv.contact_id == appt.contact_id:
            return conv
    return (
        await session.execute(
            select(Conversation)
            .where(Conversation.tenant_id == appt.tenant_id, Conversation.contact_id == appt.contact_id)
            .order_by(Conversation.last_message_at.desc().nulls_last())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _number_id(
    session: AsyncSession, tenant_id: uuid.UUID, conv: Conversation | None
) -> uuid.UUID | None:
    if conv is not None:
        return conv.number_id
    return await session.scalar(
        select(WhatsAppNumber.id)
        .where(WhatsAppNumber.tenant_id == tenant_id, WhatsAppNumber.status == NumberStatus.CONNECTED)
        .order_by(WhatsAppNumber.created_at)
        .limit(1)
    )


async def _queue(session: AsyncSession, tenant_id: uuid.UUID, req: outbound.OutboundRequest) -> uuid.UUID:
    msg = await outbound.queue_message(session, tenant_id, req)
    job = await outbound.outbox_job_for(session, msg.id)
    if job is not None:
        run_after_commit(session, lambda: outbound.dispatch_job(job))
    return msg.id


async def notify(
    session: AsyncSession, tenant: Tenant, appt: Appointment, body: str, *, user_id: uuid.UUID | None
) -> str | None:
    """Send a plain text update about this appointment. Returns None when queued, or why
    it could not be sent (usually: the customer has not written in the last 24 hours)."""
    conv = await _conversation(session, appt)
    number_id = await _number_id(session, tenant.id, conv)
    if number_id is None:
        return "no_whatsapp_number"
    if not service_window_open(conv, datetime.now(UTC)):
        return "window_closed"
    savepoint = await session.begin_nested()
    try:
        await _queue(
            session,
            tenant.id,
            outbound.OutboundRequest(
                number_id=number_id,
                contact_id=appt.contact_id,
                text=body,
                sender_type=SenderType.AGENT if user_id else SenderType.SYSTEM,
                sent_by_user_id=user_id,
            ),
        )
    except AppError as exc:
        await savepoint.rollback()
        return exc.code
    await savepoint.commit()
    return None


def reminder_values(appt: Appointment, resource: BookableResource, tenant: Tenant) -> list[str]:
    tz = slots.zone(tenant.timezone)
    local = appt.starts_at.astimezone(tz)
    return [
        appt.patient_name,
        resource_label(resource),
        slots.nice_date(local.date()),
        slots.nice_time(appt.starts_at, tz),
        appt.reference,
        tenant.name,
    ]


def template_variable_count(template: MessageTemplate) -> int:
    body = next(
        (
            str(c.get("text", ""))
            for c in template.components or []
            if str(c.get("type", "")).upper() == "BODY"
        ),
        "",
    )
    return max((int(n) for n in _VAR.findall(body)), default=0)


def reminder_text(values: list[str]) -> str:
    name, who, day, at, ref, business = values
    return (
        f"Hello {name}, this is a reminder of your appointment with {who} on {day} at {at} "
        f"(reference {ref}). Reply here if you need to change or cancel it. - {business}"
    )


async def send_reminder(tenant_id: uuid.UUID, appointment_id: uuid.UUID) -> str:
    """Send one reminder. Safe to run twice: the row is locked and marked in the same
    transaction that queues the message."""
    async with app_transaction(tenant_id) as session:
        appt = await session.get(Appointment, appointment_id, with_for_update=True)
        now = datetime.now(UTC)
        if appt is None or appt.status != AppointmentStatus.CONFIRMED or appt.reminder_status is not None:
            return "skipped"
        if appt.starts_at <= now:
            return "skipped"
        tenant = await session.get(Tenant, tenant_id)
        settings = await get_settings_row(session, tenant_id)
        resource = await session.get(BookableResource, appt.resource_id)
        assert tenant is not None and resource is not None
        if settings is None or not settings.reminders_enabled:
            return "skipped"
        values = reminder_values(appt, resource, tenant)
        conv = await _conversation(session, appt)
        number_id = await _number_id(session, tenant_id, conv)
        result, detail = "sent", None
        if number_id is None:
            result, detail = "failed", "no_whatsapp_number"
        else:
            req = outbound.OutboundRequest(
                number_id=number_id, contact_id=appt.contact_id, sender_type=SenderType.SYSTEM
            )
            if service_window_open(conv, now):
                req.text = reminder_text(values)
            elif settings.reminder_template_name:
                req.template_name = settings.reminder_template_name
                req.template_language = settings.reminder_template_language
                count = await _template_variables(
                    session,
                    tenant_id,
                    number_id,
                    settings.reminder_template_name,
                    settings.reminder_template_language,
                )
                if count > TEMPLATE_VARIABLES:
                    result, detail = "failed", "template_has_too_many_variables"
                elif count:
                    req.template_components = [
                        {
                            "type": "body",
                            "parameters": [{"type": "text", "text": v} for v in values[:count]],
                        }
                    ]
            else:
                result, detail = "skipped", "no_template"
            if result == "sent":
                savepoint = await session.begin_nested()
                try:
                    await _queue(session, tenant_id, req)
                    await savepoint.commit()
                except AppError as exc:
                    await savepoint.rollback()
                    result, detail = "failed", exc.code
        appt.reminder_status = result
        appt.reminder_detail = detail
        appt.reminder_sent_at = now if result == "sent" else None
        log.info("appointment_reminder", appointment_id=str(appointment_id), result=result, detail=detail)
        return result if detail is None else f"{result}:{detail}"


async def _template_variables(
    session: AsyncSession, tenant_id: uuid.UUID, number_id: uuid.UUID, name: str, language: str | None
) -> int:
    number = await session.get(WhatsAppNumber, number_id)
    if number is None:
        return 0
    stmt = select(MessageTemplate).where(
        MessageTemplate.tenant_id == tenant_id,
        MessageTemplate.account_id == number.account_id,
        MessageTemplate.name == name,
    )
    if language:
        stmt = stmt.where(MessageTemplate.language == language)
    template = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    return template_variable_count(template) if template is not None else 0


async def sweep(limit: int = 200) -> dict[str, Any]:
    """Reminders that are due across every business (found through a definer function,
    since this crosses tenants), sent one by one."""
    async with app_transaction() as session:
        due = (
            await session.execute(
                text("SELECT tenant_id, appointment_id FROM app_appointments_due_reminder(:l)"), {"l": limit}
            )
        ).all()
    results: dict[str, int] = {}
    for tenant_id, appointment_id in due:
        try:
            outcome = await send_reminder(tenant_id, appointment_id)
        except Exception:
            log.exception("appointment_reminder_failed", appointment_id=str(appointment_id))
            outcome = "error"
        key = outcome.split(":")[0]
        results[key] = results.get(key, 0) + 1
    return {"due": len(due), **results}
