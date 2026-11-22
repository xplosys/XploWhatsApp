"""Booking rules shared by the dashboard and the WhatsApp assistant.

Bookings for one doctor or service are serialised by locking its row, so two
customers can never take the last place in a slot at the same moment. The assistant
only proposes an action; everything is checked here again before anything changes.
"""

from __future__ import annotations

import secrets
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.modules.ai.prompt import neutralize
from app.modules.ai.validator import BookingRequest
from app.modules.appointments import slots
from app.modules.appointments.models import (
    Appointment,
    AppointmentSource,
    AppointmentStatus,
    BookableResource,
    BookingSettings,
    TimeOff,
)
from app.modules.conversations.models import Contact, Conversation
from app.modules.tenants.models import Tenant

REFERENCE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
REFERENCE_LENGTH = 6
# Upper bound for the free-times part of the assistant's prompt.
AVAILABILITY_MAX_CHARS = 5000
SUGGESTIONS = 8


class BookingError(Conflict):
    code = "booking_failed"
    message = "That appointment could not be booked."


# ---------------------------------------------------------------------- lookups


async def get_settings_row(session: AsyncSession, tenant_id: uuid.UUID) -> BookingSettings | None:
    return (
        await session.execute(select(BookingSettings).where(BookingSettings.tenant_id == tenant_id))
    ).scalar_one_or_none()


async def get_resource(
    session: AsyncSession, tenant_id: uuid.UUID, resource_id: uuid.UUID, *, lock: bool = False
) -> BookableResource:
    resource = await session.get(BookableResource, resource_id, with_for_update=lock)
    if resource is None or resource.tenant_id != tenant_id:
        raise NotFound("Doctor or service not found.", code="resource_not_found")
    return resource


async def get_appointment(
    session: AsyncSession, tenant_id: uuid.UUID, appointment_id: uuid.UUID, *, lock: bool = False
) -> Appointment:
    appt = await session.get(Appointment, appointment_id, with_for_update=lock)
    if appt is None or appt.tenant_id != tenant_id:
        raise NotFound("Appointment not found.", code="appointment_not_found")
    return appt


async def list_resources(
    session: AsyncSession, tenant_id: uuid.UUID, *, active_only: bool = False
) -> list[BookableResource]:
    stmt = select(BookableResource).where(BookableResource.tenant_id == tenant_id)
    if active_only:
        stmt = stmt.where(BookableResource.active.is_(True))
    stmt = stmt.order_by(BookableResource.department, BookableResource.name, BookableResource.id)
    return list((await session.execute(stmt)).scalars())


async def days_off(
    session: AsyncSession, tenant_id: uuid.UUID, first: date, last: date
) -> list[slots.DayOff]:
    rows = (
        await session.execute(
            select(TimeOff).where(
                TimeOff.tenant_id == tenant_id, TimeOff.starts_on <= last, TimeOff.ends_on >= first
            )
        )
    ).scalars()
    return [slots.DayOff(r.resource_id, r.starts_on, r.ends_on) for r in rows]


async def busy_by_resource(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    start: datetime,
    end: datetime,
    *,
    resource_ids: list[uuid.UUID] | None = None,
    exclude_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, list[slots.Busy]]:
    stmt = select(Appointment.resource_id, Appointment.starts_at, Appointment.ends_at).where(
        Appointment.tenant_id == tenant_id,
        Appointment.status == AppointmentStatus.CONFIRMED,
        Appointment.starts_at < end,
        Appointment.ends_at > start,
    )
    if resource_ids is not None:
        stmt = stmt.where(Appointment.resource_id.in_(resource_ids))
    if exclude_id is not None:
        stmt = stmt.where(Appointment.id != exclude_id)
    out: dict[uuid.UUID, list[slots.Busy]] = defaultdict(list)
    for resource_id, s, e in (await session.execute(stmt)).all():
        out[resource_id].append(slots.Busy(s, e))
    return out


def local_today(tz: ZoneInfo, now: datetime) -> date:
    return now.astimezone(tz).date()


def earliest_start(settings: BookingSettings | None, now: datetime) -> datetime:
    notice = settings.min_notice_minutes if settings else 0
    return now + timedelta(minutes=notice)


async def free_times(
    session: AsyncSession,
    tenant: Tenant,
    resources: list[BookableResource],
    *,
    first_day: date,
    days: int,
    earliest: datetime,
    exclude_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, dict[date, list[datetime]]]:
    """Free start times per resource and local day."""
    tz = slots.zone(tenant.timezone)
    last_day = first_day + timedelta(days=days - 1)
    window_start = slots.local_to_utc(first_day, 0, tz)
    window_end = slots.local_to_utc(last_day, 24 * 60, tz)
    off = await days_off(session, tenant.id, first_day, last_day)
    busy = await busy_by_resource(
        session,
        tenant.id,
        window_start - timedelta(hours=12),
        window_end + timedelta(hours=12),
        resource_ids=[r.id for r in resources],
        exclude_id=exclude_id,
    )
    out: dict[uuid.UUID, dict[date, list[datetime]]] = {}
    for r in resources:
        per_day: dict[date, list[datetime]] = {}
        for offset in range(days):
            day = first_day + timedelta(days=offset)
            per_day[day] = slots.free_starts(
                weekly_hours=r.weekly_hours,
                slot_minutes=r.slot_minutes,
                capacity=r.capacity,
                day=day,
                tz=tz,
                busy=busy.get(r.id, []),
                earliest=earliest,
                resource_id=r.id,
                days_off=off,
            )
        out[r.id] = per_day
    return out


def display_name(contact: Contact | None) -> str:
    if contact is None:
        return ""
    return contact.name or contact.profile_name or contact.username or ""


async def _new_reference(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    for _ in range(10):
        ref = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH))
        taken = await session.scalar(
            select(Appointment.id).where(Appointment.tenant_id == tenant_id, Appointment.reference == ref)
        )
        if taken is None:
            return ref
    raise Conflict("Could not create a reference. Try again.", code="reference_exhausted")


async def upcoming_for_contact(
    session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID, now: datetime
) -> list[Appointment]:
    return list(
        (
            await session.execute(
                select(Appointment)
                .where(
                    Appointment.tenant_id == tenant_id,
                    Appointment.contact_id == contact_id,
                    Appointment.status == AppointmentStatus.CONFIRMED,
                    Appointment.starts_at > now,
                )
                .order_by(Appointment.starts_at)
                .limit(20)
            )
        ).scalars()
    )


# ---------------------------------------------------------------------- booking


@dataclass
class BookingInput:
    resource_id: uuid.UUID
    contact: Contact
    starts_at: datetime
    patient_name: str
    notes: str = ""
    source: str = AppointmentSource.STAFF
    user_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    duration_minutes: int | None = None
    # The assistant must keep to opening hours, notice and horizon; staff need not.
    check_schedule: bool = False
    allow_overbook: bool = False


async def _check_slot(
    session: AsyncSession,
    tenant: Tenant,
    resource: BookableResource,
    contact_id: uuid.UUID,
    starts_at: datetime,
    ends_at: datetime,
    *,
    source: str,
    check_schedule: bool,
    allow_overbook: bool,
    replacing_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """Raise BookingError unless this time can be booked. The caller holds the lock on
    the resource row."""
    settings = await get_settings_row(session, tenant.id)
    tz = slots.zone(tenant.timezone)
    if not resource.active:
        raise BookingError(f"{resource.name} is not taking appointments.", code="resource_inactive")
    if starts_at <= now:
        raise BookingError("That time has already passed.", code="in_the_past")
    if check_schedule:
        day = starts_at.astimezone(tz).date()
        if settings is not None and starts_at < earliest_start(settings, now):
            raise BookingError("That time is too soon to book.", code="too_soon")
        horizon = settings.horizon_days if settings else 14
        if day >= local_today(tz, now) + timedelta(days=horizon):
            raise BookingError("That date is too far ahead to book.", code="too_far")
        if slots.is_day_off(day, resource.id, await days_off(session, tenant.id, day, day)):
            raise BookingError(f"{resource.name} is not available on that day.", code="day_off")
        if starts_at not in slots.day_starts(resource.weekly_hours, day, resource.slot_minutes, tz):
            raise BookingError(f"{resource.name} does not see anyone at that time.", code="outside_hours")
    if not allow_overbook:
        busy = await busy_by_resource(
            session, tenant.id, starts_at, ends_at, resource_ids=[resource.id], exclude_id=replacing_id
        )
        if slots.overlapping(starts_at, ends_at, busy.get(resource.id, [])) >= resource.capacity:
            raise BookingError("That time has just been taken.", code="slot_taken")
    if source == AppointmentSource.AI and replacing_id is None:
        limit = settings.max_upcoming_per_contact if settings else 3
        upcoming = await upcoming_for_contact(session, tenant.id, contact_id, now)
        if len(upcoming) >= limit:
            raise BookingError(
                f"This number already has {len(upcoming)} upcoming appointments.", code="too_many_upcoming"
            )


async def book(
    session: AsyncSession, tenant: Tenant, data: BookingInput, *, now: datetime | None = None
) -> tuple[Appointment, bool]:
    """Book an appointment. Returns (appointment, created); an identical confirmed
    booking for the same person is returned instead of a duplicate. Raises BookingError."""
    now = now or datetime.now(UTC)
    resource = await get_resource(session, tenant.id, data.resource_id, lock=True)  # serialises the slot
    starts_at = data.starts_at.astimezone(UTC)
    ends_at = starts_at + timedelta(minutes=data.duration_minutes or resource.slot_minutes)
    same = (
        await session.execute(
            select(Appointment).where(
                Appointment.tenant_id == tenant.id,
                Appointment.contact_id == data.contact.id,
                Appointment.resource_id == resource.id,
                Appointment.starts_at == starts_at,
                Appointment.status == AppointmentStatus.CONFIRMED,
            )
        )
    ).scalar_one_or_none()
    if same is not None:
        return same, False
    await _check_slot(
        session,
        tenant,
        resource,
        data.contact.id,
        starts_at,
        ends_at,
        source=data.source,
        check_schedule=data.check_schedule,
        allow_overbook=data.allow_overbook,
        replacing_id=None,
        now=now,
    )
    appt = Appointment(
        tenant_id=tenant.id,
        resource_id=resource.id,
        contact_id=data.contact.id,
        conversation_id=data.conversation_id,
        reference=await _new_reference(session, tenant.id),
        patient_name=(data.patient_name.strip() or display_name(data.contact) or "Customer")[:120],
        starts_at=starts_at,
        ends_at=ends_at,
        status=AppointmentStatus.CONFIRMED,
        source=data.source,
        notes=data.notes.strip()[:1000],
        created_by_user_id=data.user_id,
    )
    session.add(appt)
    await session.flush()
    return appt, True


async def move(
    session: AsyncSession,
    tenant: Tenant,
    appt: Appointment,
    *,
    resource_id: uuid.UUID,
    starts_at: datetime,
    source: str,
    check_schedule: bool = False,
    allow_overbook: bool = False,
    now: datetime | None = None,
) -> datetime:
    """Move a confirmed appointment to a new time (and maybe another doctor or service),
    keeping its reference. Returns the previous start. Raises BookingError."""
    now = now or datetime.now(UTC)
    resource = await get_resource(session, tenant.id, resource_id, lock=True)
    starts_at = starts_at.astimezone(UTC)
    length = (
        appt.ends_at - appt.starts_at
        if resource_id == appt.resource_id
        else timedelta(minutes=resource.slot_minutes)
    )
    await _check_slot(
        session,
        tenant,
        resource,
        appt.contact_id,
        starts_at,
        starts_at + length,
        source=source,
        check_schedule=check_schedule,
        allow_overbook=allow_overbook,
        replacing_id=appt.id,
        now=now,
    )
    previous = appt.starts_at
    appt.resource_id = resource.id
    appt.starts_at, appt.ends_at = starts_at, starts_at + length
    appt.times_moved += 1
    # A new time deserves its own reminder.
    appt.reminder_status = appt.reminder_detail = None
    appt.reminder_sent_at = None
    await session.flush()
    return previous


def cancel(appt: Appointment, *, by: str, now: datetime | None = None) -> None:
    appt.status = AppointmentStatus.CANCELLED
    appt.cancelled_at = now or datetime.now(UTC)
    appt.cancelled_by = by


# ---------------------------------------------------------------------- messages


def when(appt_start: datetime, tz: ZoneInfo) -> str:
    local = appt_start.astimezone(tz)
    return f"{slots.nice_date(local.date())}, {slots.nice_time(appt_start, tz)}"


def resource_label(resource: BookableResource) -> str:
    return f"{resource.name} ({resource.department})" if resource.department else resource.name


def confirmation_block(
    appt: Appointment, resource: BookableResource, tz: ZoneInfo, settings: BookingSettings | None
) -> str:
    lines = [
        "*Appointment confirmed*",
        f"With: {resource_label(resource)}",
        f"When: {when(appt.starts_at, tz)}",
        f"Patient: {appt.patient_name}",
        f"Reference: *{appt.reference}*",
    ]
    if settings is not None and settings.confirmation_note:
        lines += ["", settings.confirmation_note]
    lines += ["", "To cancel or change it, just message us here."]
    return "\n".join(lines)


def cancelled_block(appt: Appointment, resource: BookableResource, tz: ZoneInfo) -> str:
    return "\n".join(
        [
            "*Appointment cancelled*",
            f"With: {resource_label(resource)}",
            f"When: {when(appt.starts_at, tz)}",
            f"Reference: {appt.reference}",
        ]
    )


# ---------------------------------------------------------------------- assistant


@dataclass
class BookingContext:
    """What the assistant is told about booking for one reply, and what its answer may
    refer to. Built while preparing the reply, checked again when acting on it."""

    prompt: str
    grounding: str
    codes: dict[str, uuid.UUID] = field(default_factory=dict)


def _code_map(resources: list[BookableResource]) -> dict[str, BookableResource]:
    return {f"R{i}": r for i, r in enumerate(resources, start=1)}


async def ai_context(
    session: AsyncSession, tenant: Tenant, contact_id: uuid.UUID | None, *, now: datetime | None = None
) -> BookingContext | None:
    """None unless the business lets the assistant book and has someone to book with."""
    now = now or datetime.now(UTC)
    settings = await get_settings_row(session, tenant.id)
    if settings is None or not settings.ai_booking_enabled:
        return None
    resources = await list_resources(session, tenant.id, active_only=True)
    if not resources:
        return None
    tz = slots.zone(tenant.timezone)
    today = local_today(tz, now)
    codes = _code_map(resources)
    free = await free_times(
        session,
        tenant,
        resources,
        first_day=today,
        days=settings.horizon_days,
        earliest=earliest_start(settings, now),
    )

    local_now = now.astimezone(tz)
    lines = [
        f"Today is {slots.WEEKDAY_NAMES[slots.WEEKDAYS[today.weekday()]]} {slots.nice_date(today)} "
        f"({today.isoformat()}), and the time is {local_now.strftime('%H:%M')} ({tenant.timezone}).",
        "",
        "Doctors and services that can be booked (code, name, department, details):",
    ]
    for code, r in codes.items():
        parts = [f"[{code}] {neutralize(r.name)}"]
        if r.department:
            parts.append(f"department: {neutralize(r.department)}")
        if r.description:
            parts.append(neutralize(r.description))
        parts.append(f"{r.slot_minutes}-minute appointments")
        lines.append(" - ".join(parts))

    lines += [
        "",
        f"Free start times for the next {settings.horizon_days} days (24-hour clock). "
        "A range such as 10:00-12:00 means every start time in it, one per appointment length. "
        "Days that are not listed have no free times.",
    ]
    grounding: list[str] = []
    used = sum(len(x) + 1 for x in lines)
    cut = False
    for code, r in codes.items():
        block = [f"[{code}] {neutralize(r.name)} (a new time every {r.slot_minutes} minutes):"]
        times: list[str] = []
        for day, starts in free[r.id].items():
            if not starts:
                continue
            ranges = slots.describe_runs(slots.runs(starts, r.slot_minutes, tz))
            line = f"  {slots.nice_date(day)} ({day.isoformat()}): {ranges}"
            if used + sum(len(x) + 1 for x in block) + len(line) > AVAILABILITY_MAX_CHARS:
                cut = True
                break
            block.append(line)
            times += [s.astimezone(tz).strftime("%H:%M") for s in starts]
        if len(block) == 1:
            block.append("  No free times in this period.")
        lines += block
        used += sum(len(x) + 1 for x in block)
        grounding.append(" ".join(times))
    if cut:
        lines.append("(Later dates are not shown. If the customer wants one, say the team will confirm it.)")

    mine: list[Appointment] = []
    if contact_id is not None:
        mine = await upcoming_for_contact(session, tenant.id, contact_id, now)
    by_id: dict[uuid.UUID, tuple[str, BookableResource | None]] = {
        r.id: (code, r) for code, r in codes.items()
    }
    lines += ["", "This customer's upcoming appointments:"]
    if not mine:
        lines.append("  None.")
    for a in mine:
        code, found = by_id.get(a.resource_id, ("", None))
        name = neutralize(found.name) if found else "a doctor or service that is no longer listed"
        local = a.starts_at.astimezone(tz)
        lines.append(
            f"  Reference {a.reference}: {name}{f' [{code}]' if code else ''}, "
            f"{slots.nice_date(local.date())} ({local.date().isoformat()}) at {local.strftime('%H:%M')}, "
            f"patient {neutralize(a.patient_name)}."
        )
        grounding.append(local.strftime("%H:%M"))
    return BookingContext(
        prompt="\n".join(lines), grounding="\n".join(grounding), codes={c: r.id for c, r in codes.items()}
    )


@dataclass
class ActionResult:
    ok: bool
    text: str
    details: dict[str, Any] = field(default_factory=dict)


async def _suggest(
    session: AsyncSession, tenant: Tenant, resource: BookableResource, day: date | None, now: datetime
) -> str:
    """Free times on the asked-for day, or the next day that has any."""
    settings = await get_settings_row(session, tenant.id)
    tz = slots.zone(tenant.timezone)
    today = local_today(tz, now)
    horizon = settings.horizon_days if settings else 14
    start = day if day is not None and today <= day < today + timedelta(days=horizon) else today
    days = (today + timedelta(days=horizon) - start).days
    if days <= 0:
        return ""
    free = (
        await free_times(
            session, tenant, [resource], first_day=start, days=days, earliest=earliest_start(settings, now)
        )
    )[resource.id]
    for d, starts in free.items():
        if starts:
            shown = ", ".join(slots.nice_time(s, tz) for s in starts[:SUGGESTIONS])
            more = " and more" if len(starts) > SUGGESTIONS else ""
            label = "that day" if d == day else slots.nice_date(d)
            return f"Free times with {resource.name} {'on ' if d != day else ''}{label}: {shown}{more}."
    return f"{resource.name} has no free times in the next {horizon} days."


FAILURE_TEXT = {
    "slot_taken": "Sorry, that time with {name} has just been taken.",
    "outside_hours": "Sorry, {name} does not see anyone at that time.",
    "day_off": "Sorry, {name} is not available on that day.",
    "too_soon": "Sorry, that time is too soon to book.",
    "too_far": "Sorry, I can only book up to {horizon} days ahead.",
    "in_the_past": "Sorry, that time has already passed.",
    "resource_inactive": "Sorry, {name} is not taking appointments at the moment.",
}


async def apply_ai_action(
    session: AsyncSession,
    tenant: Tenant,
    conversation: Conversation,
    context: BookingContext,
    request: BookingRequest,
    *,
    reply: str,
    now: datetime | None = None,
) -> ActionResult:
    """Carry out what the assistant asked for, if the rules allow it, and return the
    message the customer should get."""
    now = now or datetime.now(UTC)
    tz = slots.zone(tenant.timezone)
    contact = await session.get(Contact, conversation.contact_id)
    assert contact is not None
    settings = await get_settings_row(session, tenant.id)
    base = {"action": request.action}

    old: Appointment | None = None
    if request.action in ("cancel", "reschedule"):
        ref = request.reference.strip().upper().removeprefix("#")
        old = (
            await session.execute(
                select(Appointment)
                .where(
                    Appointment.tenant_id == tenant.id,
                    Appointment.contact_id == contact.id,
                    func.upper(Appointment.reference) == ref,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if old is None or old.status != AppointmentStatus.CONFIRMED or old.starts_at <= now:
            return ActionResult(
                False,
                "Sorry, I couldn't find an upcoming appointment with that reference for this number. "
                "Please check the reference, or ask for a member of our team.",
                {**base, "result": "not_found", "reference": ref},
            )
        if request.action == "cancel":
            resource = await get_resource(session, tenant.id, old.resource_id)
            cancel(old, by="customer", now=now)
            text = cancelled_block(old, resource, tz)
            return ActionResult(
                True,
                f"{reply}\n\n{text}" if reply else text,
                {**base, "result": "cancelled", "appointment_id": str(old.id), "reference": old.reference},
            )

    resource_id = context.codes.get(request.resource.strip().upper().strip("[]"))
    if resource_id is None and old is not None and not request.resource:
        resource_id = old.resource_id  # "move it to 4pm" keeps the same doctor
    day = slots.parse_date(request.date)
    clock = slots.parse_clock(request.time)
    if resource_id is None or day is None or clock is None:
        return ActionResult(
            False,
            "Sorry, I couldn't book that. "
            "Could you tell me again which doctor or service, the date and the time?",
            {
                **base,
                "result": "unclear",
                "resource": request.resource,
                "date": request.date,
                "time": request.time,
            },
        )
    resource = await get_resource(session, tenant.id, resource_id)
    starts_at = slots.local_to_utc(day, clock, tz)
    try:
        if old is not None:
            previous = await move(
                session,
                tenant,
                old,
                resource_id=resource.id,
                starts_at=starts_at,
                source=AppointmentSource.AI,
                check_schedule=True,
                now=now,
            )
            if request.notes and not old.notes:
                old.notes = request.notes[:1000]
            appt, created = old, True
        else:
            appt, created = await book(
                session,
                tenant,
                BookingInput(
                    resource_id=resource.id,
                    contact=contact,
                    starts_at=starts_at,
                    patient_name=request.patient_name,
                    notes=request.notes,
                    source=AppointmentSource.AI,
                    conversation_id=conversation.id,
                    check_schedule=True,
                ),
                now=now,
            )
    except BookingError as exc:
        horizon = settings.horizon_days if settings else 14
        if exc.code == "too_many_upcoming":
            return ActionResult(
                False,
                "Sorry, this number already has the most upcoming appointments we can book "
                f"({settings.max_upcoming_per_contact if settings else 3}). Please cancel one first, "
                "or ask for a member of our team.",
                {**base, "result": exc.code},
            )
        sorry = FAILURE_TEXT.get(exc.code, "Sorry, I couldn't book that.").format(
            name=resource.name, horizon=horizon
        )
        hint = await _suggest(session, tenant, resource, day, now)
        question = " Which time would you like?" if hint and "no free times" not in hint else ""
        return ActionResult(
            False,
            f"{sorry} {hint}{question}".strip(),
            {**base, "result": exc.code, "resource_id": str(resource.id), "starts_at": starts_at.isoformat()},
        )
    details: dict[str, Any] = {
        **base,
        "result": "booked" if created else "already_booked",
        "appointment_id": str(appt.id),
        "reference": appt.reference,
    }
    text = confirmation_block(appt, resource, tz, settings)
    if old is not None:
        details["result"] = "rescheduled"
        details["previous_starts_at"] = previous.isoformat()
        text = text.replace("*Appointment confirmed*", "*Appointment moved*", 1) + (
            f"\n(Your earlier time, {when(previous, tz)}, is no longer booked.)"
        )
    return ActionResult(True, f"{reply}\n\n{text}" if reply else text, details)
