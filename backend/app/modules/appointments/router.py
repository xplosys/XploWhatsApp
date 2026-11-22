"""Appointments: the calendar, doctors and services with their hours, days off, and the
booking settings that let the WhatsApp assistant book by itself."""

from __future__ import annotations

import datetime as dt
import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select

from app.api.deps import Meta, Requires, TenantContext
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.modules.appointments import messaging, service, slots
from app.modules.appointments.models import (
    Appointment,
    AppointmentSource,
    AppointmentStatus,
    BookableResource,
    BookingSettings,
    TimeOff,
)
from app.modules.audit.service import record
from app.modules.conversations import inbox
from app.modules.conversations import service as conv
from app.modules.conversations.models import Contact
from app.modules.conversations.schemas import UserRef, user_names, user_ref
from app.modules.rbac.permissions import Permission

router = APIRouter(prefix="/appointments", tags=["appointments"])
Staff = Annotated[TenantContext, Requires(Permission.APPOINTMENTS_MANAGE)]
Configurer = Annotated[TenantContext, Requires(Permission.APPOINTMENTS_CONFIGURE)]

MAX_RANGE_DAYS = 62
HHMM = Field(pattern=r"^([01]?\d|2[0-3]):[0-5]\d$")


# ---------------------------------------------------------------------- schemas


class SettingsIn(BaseModel):
    ai_booking_enabled: bool = False
    horizon_days: int = Field(default=14, ge=1, le=90)
    min_notice_minutes: int = Field(default=60, ge=0, le=10080)
    max_upcoming_per_contact: int = Field(default=3, ge=1, le=10)
    confirmation_note: str = Field(default="", max_length=500)
    reminders_enabled: bool = False
    reminder_hours_before: int = Field(default=24, ge=1, le=168)
    reminder_template_name: str | None = Field(default=None, max_length=512)
    reminder_template_language: str | None = Field(default=None, max_length=16)

    @field_validator("confirmation_note")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("reminder_template_name", "reminder_template_language")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class SettingsOut(SettingsIn):
    timezone: str
    active_resources: int


class ResourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    department: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=500)
    slot_minutes: int = Field(default=30, ge=5, le=480)
    capacity: int = Field(default=1, ge=1, le=50)
    weekly_hours: dict[str, list[list[str]]] = Field(default_factory=dict)
    active: bool = True

    @field_validator("name", "department", "description")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("weekly_hours")
    @classmethod
    def _hours(cls, value: dict[str, list[list[str]]]) -> dict[str, list[list[str]]]:
        return slots.clean_weekly_hours(value)


class ResourceOut(ResourceIn):
    id: uuid.UUID
    upcoming: int
    created_at: datetime


class TimeOffIn(BaseModel):
    resource_id: uuid.UUID | None = None
    starts_on: date
    ends_on: date
    reason: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _ordered(self) -> TimeOffIn:
        if self.ends_on < self.starts_on:
            raise ValueError("The last day must be on or after the first day.")
        if (self.ends_on - self.starts_on).days > 366:
            raise ValueError("Time off can be at most a year at a time.")
        return self


class TimeOffOut(TimeOffIn):
    id: uuid.UUID
    resource_name: str | None


class ResourceRef(BaseModel):
    id: uuid.UUID
    name: str
    department: str


class ContactRef(BaseModel):
    id: uuid.UUID
    name: str | None
    phone_e164: str | None


class AppointmentOut(BaseModel):
    id: uuid.UUID
    reference: str
    resource: ResourceRef
    contact: ContactRef
    conversation_id: uuid.UUID | None
    patient_name: str
    starts_at: datetime
    ends_at: datetime
    # Start in the business's time zone, "2026-11-23T10:00", for display.
    local_start: str
    status: str
    source: str
    notes: str
    created_by: UserRef | None
    cancelled_at: datetime | None
    cancelled_by: str | None
    reminder_status: str | None
    reminder_detail: str | None
    reminder_sent_at: datetime | None
    created_at: datetime


class AppointmentResult(AppointmentOut):
    # When the customer was to be told on WhatsApp: whether it was queued, and if not, why.
    notified: bool | None = None
    notify_error: str | None = None


class AppointmentList(BaseModel):
    items: list[AppointmentOut]
    timezone: str


class AppointmentCreate(BaseModel):
    resource_id: uuid.UUID
    contact_id: uuid.UUID | None = None
    phone: str | None = Field(default=None, max_length=32)
    date: dt.date
    time: Annotated[str, HHMM]
    patient_name: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=1000)
    allow_overbook: bool = False
    notify: bool = False

    @model_validator(mode="after")
    def _who(self) -> AppointmentCreate:
        if bool(self.contact_id) == bool(self.phone):
            raise ValueError("Give either contact_id or phone.")
        return self


class AppointmentUpdate(BaseModel):
    status: Literal["confirmed", "completed", "no_show"] | None = None
    patient_name: str | None = Field(default=None, min_length=1, max_length=120)
    notes: str | None = Field(default=None, max_length=1000)
    # Move it: give the new date and time (and optionally another doctor or service).
    resource_id: uuid.UUID | None = None
    date: dt.date | None = None
    time: Annotated[str, HHMM] | None = None
    allow_overbook: bool = False
    notify: bool = False

    @model_validator(mode="after")
    def _move(self) -> AppointmentUpdate:
        if (self.date is None) != (self.time is None):
            raise ValueError("Give both date and time to move an appointment.")
        return self


class CancelIn(BaseModel):
    notify: bool = True


class DayTimes(BaseModel):
    date: dt.date
    times: list[str]  # "HH:MM" in the business's time zone


class ResourceAvailability(BaseModel):
    resource_id: uuid.UUID
    days: list[DayTimes]


# ---------------------------------------------------------------------- helpers


def _settings_out(row: BookingSettings | None, timezone: str, active: int) -> SettingsOut:
    base = SettingsIn() if row is None else SettingsIn.model_validate(row, from_attributes=True)
    return SettingsOut(**base.model_dump(), timezone=timezone, active_resources=active)


async def _active_count(ctx: TenantContext) -> int:
    return int(
        await ctx.session.scalar(
            select(func.count())
            .select_from(BookableResource)
            .where(BookableResource.tenant_id == ctx.tenant_id, BookableResource.active.is_(True))
        )
        or 0
    )


async def _resource_out(ctx: TenantContext, resources: list[BookableResource]) -> list[ResourceOut]:
    counts = dict(
        (
            await ctx.session.execute(
                select(Appointment.resource_id, func.count())
                .where(
                    Appointment.tenant_id == ctx.tenant_id,
                    Appointment.status == AppointmentStatus.CONFIRMED,
                    Appointment.starts_at > datetime.now(UTC),
                )
                .group_by(Appointment.resource_id)
            )
        ).all()
    )
    return [
        ResourceOut(
            id=r.id,
            name=r.name,
            department=r.department,
            description=r.description,
            slot_minutes=r.slot_minutes,
            capacity=r.capacity,
            weekly_hours=r.weekly_hours,
            active=r.active,
            upcoming=int(counts.get(r.id, 0)),
            created_at=r.created_at,
        )
        for r in resources
    ]


async def _appointments_out(ctx: TenantContext, rows: list[Appointment]) -> list[AppointmentOut]:
    tz = slots.zone(ctx.tenant.timezone)
    resource_ids = {a.resource_id for a in rows}
    contact_ids = {a.contact_id for a in rows}
    resources = (
        {
            r.id: r
            for r in (
                await ctx.session.execute(
                    select(BookableResource).where(BookableResource.id.in_(resource_ids))
                )
            ).scalars()
        }
        if resource_ids
        else {}
    )
    contacts = (
        {
            c.id: c
            for c in (await ctx.session.execute(select(Contact).where(Contact.id.in_(contact_ids)))).scalars()
        }
        if contact_ids
        else {}
    )
    names = await user_names(ctx.session, [a.created_by_user_id for a in rows])
    out = []
    for a in rows:
        r = resources[a.resource_id]
        c = contacts.get(a.contact_id)
        out.append(
            AppointmentOut(
                id=a.id,
                reference=a.reference,
                resource=ResourceRef(id=r.id, name=r.name, department=r.department),
                contact=ContactRef(
                    id=a.contact_id,
                    name=service.display_name(c) or None,
                    phone_e164=c.phone_e164 if c else None,
                ),
                conversation_id=a.conversation_id,
                patient_name=a.patient_name,
                starts_at=a.starts_at,
                ends_at=a.ends_at,
                local_start=a.starts_at.astimezone(tz).strftime("%Y-%m-%dT%H:%M"),
                status=a.status,
                source=a.source,
                notes=a.notes,
                created_by=user_ref(a.created_by_user_id, names),
                cancelled_at=a.cancelled_at,
                cancelled_by=a.cancelled_by,
                reminder_status=a.reminder_status,
                reminder_detail=a.reminder_detail,
                reminder_sent_at=a.reminder_sent_at,
                created_at=a.created_at,
            )
        )
    return out


async def _result(
    ctx: TenantContext, appt: Appointment, *, notice: str | None, notify: bool
) -> AppointmentResult:
    """The appointment, after telling the customer about it if asked."""
    error = None
    if notify and notice:
        error = await messaging.notify(ctx.session, ctx.tenant, appt, notice, user_id=ctx.user.id)
    await ctx.session.flush()
    await ctx.session.refresh(appt)
    [base] = await _appointments_out(ctx, [appt])
    return AppointmentResult(
        **base.model_dump(),
        notified=(error is None) if notify else None,
        notify_error=error if notify else None,
    )


def _start(ctx: TenantContext, day: date, hhmm: str) -> datetime:
    return slots.local_to_utc(day, slots.minutes(hhmm), slots.zone(ctx.tenant.timezone))


# ---------------------------------------------------------------------- settings


@router.get("/settings", response_model=SettingsOut)
async def get_booking_settings(ctx: Staff) -> SettingsOut:
    row = await service.get_settings_row(ctx.session, ctx.tenant_id)
    return _settings_out(row, ctx.tenant.timezone, await _active_count(ctx))


@router.put("/settings", response_model=SettingsOut)
async def update_booking_settings(body: SettingsIn, ctx: Configurer, meta: Meta) -> SettingsOut:
    """Turn the assistant's booking on or off, and set the booking rules and reminders."""
    if body.reminders_enabled and body.reminder_template_name and not body.reminder_template_language:
        raise ValidationFailed("Choose the template's language.", code="template_language_required")
    row = await service.get_settings_row(ctx.session, ctx.tenant_id)
    if row is None:
        row = BookingSettings(tenant_id=ctx.tenant_id)
        ctx.session.add(row)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await ctx.session.flush()
    await record(
        ctx.session,
        "appointments.settings_updated",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="booking_settings",
        target_id=row.id,
        details={"ai_booking_enabled": body.ai_booking_enabled, "reminders_enabled": body.reminders_enabled},
    )
    return _settings_out(row, ctx.tenant.timezone, await _active_count(ctx))


# ---------------------------------------------------------------------- doctors and services


@router.get("/resources", response_model=list[ResourceOut])
async def list_resources(ctx: Staff) -> list[ResourceOut]:
    return await _resource_out(ctx, await service.list_resources(ctx.session, ctx.tenant_id))


@router.post("/resources", response_model=ResourceOut, status_code=201)
async def create_resource(body: ResourceIn, ctx: Configurer, meta: Meta) -> ResourceOut:
    resource = BookableResource(tenant_id=ctx.tenant_id, **body.model_dump())
    ctx.session.add(resource)
    await ctx.session.flush()
    await ctx.session.refresh(resource)
    await record(
        ctx.session,
        "appointments.resource_created",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="booking_resource",
        target_id=resource.id,
        details={"name": resource.name},
    )
    [out] = await _resource_out(ctx, [resource])
    return out


@router.put("/resources/{resource_id}", response_model=ResourceOut)
async def update_resource(
    resource_id: uuid.UUID, body: ResourceIn, ctx: Configurer, meta: Meta
) -> ResourceOut:
    """Change a doctor or service. Existing appointments stay as booked."""
    resource = await service.get_resource(ctx.session, ctx.tenant_id, resource_id, lock=True)
    for key, value in body.model_dump().items():
        setattr(resource, key, value)
    await ctx.session.flush()
    await ctx.session.refresh(resource)
    await record(
        ctx.session,
        "appointments.resource_updated",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="booking_resource",
        target_id=resource.id,
        details={"name": resource.name, "active": resource.active},
    )
    [out] = await _resource_out(ctx, [resource])
    return out


@router.delete("/resources/{resource_id}", status_code=204)
async def delete_resource(resource_id: uuid.UUID, ctx: Configurer, meta: Meta) -> Response:
    """Delete a doctor or service that has never been booked. Otherwise turn it off."""
    resource = await service.get_resource(ctx.session, ctx.tenant_id, resource_id, lock=True)
    booked = await ctx.session.scalar(
        select(Appointment.id)
        .where(Appointment.tenant_id == ctx.tenant_id, Appointment.resource_id == resource.id)
        .limit(1)
    )
    if booked is not None:
        raise Conflict(
            "This doctor or service has appointments, so it can't be deleted. Turn it off instead.",
            code="resource_has_appointments",
        )
    await record(
        ctx.session,
        "appointments.resource_deleted",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="booking_resource",
        target_id=resource.id,
        details={"name": resource.name},
    )
    await ctx.session.delete(resource)
    return Response(status_code=204)


# ---------------------------------------------------------------------- time off


@router.get("/time-off", response_model=list[TimeOffOut])
async def list_time_off(ctx: Staff, include_past: bool = False) -> list[TimeOffOut]:
    stmt = select(TimeOff).where(TimeOff.tenant_id == ctx.tenant_id)
    if not include_past:
        today = service.local_today(slots.zone(ctx.tenant.timezone), datetime.now(UTC))
        stmt = stmt.where(TimeOff.ends_on >= today)
    rows = list(
        (await ctx.session.execute(stmt.order_by(TimeOff.starts_on, TimeOff.id).limit(500))).scalars()
    )
    names = {r.id: r.name for r in await service.list_resources(ctx.session, ctx.tenant_id)}
    return [
        TimeOffOut(
            id=r.id,
            resource_id=r.resource_id,
            resource_name=names.get(r.resource_id) if r.resource_id else None,
            starts_on=r.starts_on,
            ends_on=r.ends_on,
            reason=r.reason,
        )
        for r in rows
    ]


@router.post("/time-off", response_model=TimeOffOut, status_code=201)
async def create_time_off(body: TimeOffIn, ctx: Configurer, meta: Meta) -> TimeOffOut:
    """Close whole days for everyone (no resource) or for one doctor or service. Existing
    appointments on those days are not cancelled; the calendar shows them."""
    name = None
    if body.resource_id:
        name = (await service.get_resource(ctx.session, ctx.tenant_id, body.resource_id)).name
    row = TimeOff(tenant_id=ctx.tenant_id, **body.model_dump())
    row.reason = row.reason.strip()
    ctx.session.add(row)
    await ctx.session.flush()
    await record(
        ctx.session,
        "appointments.time_off_created",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="booking_time_off",
        target_id=row.id,
        details={"starts_on": str(row.starts_on), "ends_on": str(row.ends_on), "resource": name},
    )
    return TimeOffOut(
        id=row.id,
        resource_id=row.resource_id,
        resource_name=name,
        starts_on=row.starts_on,
        ends_on=row.ends_on,
        reason=row.reason,
    )


@router.delete("/time-off/{time_off_id}", status_code=204)
async def delete_time_off(time_off_id: uuid.UUID, ctx: Configurer, meta: Meta) -> Response:
    row = await ctx.session.get(TimeOff, time_off_id)
    if row is None or row.tenant_id != ctx.tenant_id:
        raise NotFound("Time off not found.", code="time_off_not_found")
    await record(
        ctx.session,
        "appointments.time_off_deleted",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="booking_time_off",
        target_id=row.id,
        details={"starts_on": str(row.starts_on), "ends_on": str(row.ends_on)},
    )
    await ctx.session.delete(row)
    return Response(status_code=204)


# ---------------------------------------------------------------------- availability


@router.get("/availability", response_model=list[ResourceAvailability])
async def availability(
    ctx: Staff,
    start: date,
    days: Annotated[int, Query(ge=1, le=31)] = 7,
    resource_id: uuid.UUID | None = None,
) -> list[ResourceAvailability]:
    """Free start times per doctor or service, as the assistant sees them (opening hours,
    days off and places left; minimum notice applies to today)."""
    resources = await service.list_resources(ctx.session, ctx.tenant_id, active_only=resource_id is None)
    if resource_id is not None:
        resources = [r for r in resources if r.id == resource_id]
        if not resources:
            raise NotFound("Doctor or service not found.", code="resource_not_found")
    settings = await service.get_settings_row(ctx.session, ctx.tenant_id)
    free = await service.free_times(
        ctx.session,
        ctx.tenant,
        resources,
        first_day=start,
        days=days,
        earliest=service.earliest_start(settings, datetime.now(UTC)),
    )
    tz = slots.zone(ctx.tenant.timezone)
    return [
        ResourceAvailability(
            resource_id=r.id,
            days=[
                DayTimes(date=d, times=[s.astimezone(tz).strftime("%H:%M") for s in starts])
                for d, starts in free[r.id].items()
            ],
        )
        for r in resources
    ]


# ---------------------------------------------------------------------- appointments


@router.get("", response_model=AppointmentList)
async def list_appointments(
    ctx: Staff,
    start: date,
    end: date,
    resource_id: uuid.UUID | None = None,
    status: Literal["confirmed", "cancelled", "completed", "no_show", "active"] | None = None,
    contact_id: uuid.UUID | None = None,
) -> AppointmentList:
    """Appointments starting on the local days start..end (inclusive), earliest first.
    `status=active` means not cancelled."""
    if end < start:
        raise ValidationFailed("end must be on or after start.", code="invalid_range")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise ValidationFailed(f"Ask for at most {MAX_RANGE_DAYS} days at a time.", code="range_too_long")
    tz = slots.zone(ctx.tenant.timezone)
    stmt = select(Appointment).where(
        Appointment.tenant_id == ctx.tenant_id,
        Appointment.starts_at >= slots.local_to_utc(start, 0, tz),
        Appointment.starts_at < slots.local_to_utc(end, 24 * 60, tz),
    )
    if resource_id:
        stmt = stmt.where(Appointment.resource_id == resource_id)
    if contact_id:
        stmt = stmt.where(Appointment.contact_id == contact_id)
    if status == "active":
        stmt = stmt.where(Appointment.status != AppointmentStatus.CANCELLED)
    elif status:
        stmt = stmt.where(Appointment.status == status)
    rows = list(
        (
            await ctx.session.execute(stmt.order_by(Appointment.starts_at, Appointment.id).limit(2000))
        ).scalars()
    )
    return AppointmentList(items=await _appointments_out(ctx, rows), timezone=ctx.tenant.timezone)


@router.get("/{appointment_id}", response_model=AppointmentOut)
async def get_appointment(appointment_id: uuid.UUID, ctx: Staff) -> AppointmentOut:
    [out] = await _appointments_out(
        ctx, [await service.get_appointment(ctx.session, ctx.tenant_id, appointment_id)]
    )
    return out


@router.post("", response_model=AppointmentResult, status_code=201)
async def create_appointment(body: AppointmentCreate, ctx: Staff, meta: Meta) -> AppointmentResult:
    """Book from the dashboard, for an existing contact or a phone number. Staff may book
    outside the published hours; a full slot needs allow_overbook."""
    if body.contact_id:
        contact = await inbox.get_contact(ctx.session, ctx.tenant_id, body.contact_id)
    else:
        wa_id = conv.normalise_wa_id(body.phone)
        if not wa_id or not 8 <= len(wa_id) <= 15:
            raise ValidationFailed(
                "Enter the phone number in international format, for example +919876543210.",
                code="invalid_phone",
            )
        contact = await conv.upsert_contact(ctx.session, ctx.tenant_id, wa_id=wa_id)
    appt, _ = await service.book(
        ctx.session,
        ctx.tenant,
        service.BookingInput(
            resource_id=body.resource_id,
            contact=contact,
            starts_at=_start(ctx, body.date, body.time),
            patient_name=body.patient_name,
            notes=body.notes,
            source=AppointmentSource.STAFF,
            user_id=ctx.user.id,
            allow_overbook=body.allow_overbook,
        ),
    )
    await record(
        ctx.session,
        "appointment.created",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="appointment",
        target_id=appt.id,
        details={"reference": appt.reference, "starts_at": appt.starts_at.isoformat()},
    )
    resource = await service.get_resource(ctx.session, ctx.tenant_id, appt.resource_id)
    settings = await service.get_settings_row(ctx.session, ctx.tenant_id)
    notice = service.confirmation_block(appt, resource, slots.zone(ctx.tenant.timezone), settings)
    return await _result(ctx, appt, notice=notice, notify=body.notify)


@router.patch("/{appointment_id}", response_model=AppointmentResult)
async def update_appointment(
    appointment_id: uuid.UUID, body: AppointmentUpdate, ctx: Staff, meta: Meta
) -> AppointmentResult:
    """Mark an appointment completed or a no-show, edit its details, or move it."""
    appt = await service.get_appointment(ctx.session, ctx.tenant_id, appointment_id, lock=True)
    if appt.status == AppointmentStatus.CANCELLED:
        raise Conflict(
            "This appointment was cancelled. Book a new one instead.", code="appointment_cancelled"
        )
    changes: dict[str, Any] = {}
    fields = body.model_fields_set
    if "patient_name" in fields and body.patient_name and body.patient_name.strip() != appt.patient_name:
        changes["patient_name"] = appt.patient_name = body.patient_name.strip()
    if "notes" in fields and body.notes is not None and body.notes.strip() != appt.notes:
        appt.notes = body.notes.strip()
        changes["notes"] = True
    notice = None
    if body.date is not None and body.time is not None:
        new_start = _start(ctx, body.date, body.time)
        new_resource = body.resource_id or appt.resource_id
        if new_start != appt.starts_at or new_resource != appt.resource_id:
            previous = await service.move(
                ctx.session,
                ctx.tenant,
                appt,
                resource_id=new_resource,
                starts_at=new_start,
                source=AppointmentSource.STAFF,
                allow_overbook=body.allow_overbook,
            )
            changes["moved"] = {"from": previous.isoformat(), "to": appt.starts_at.isoformat()}
            resource = await service.get_resource(ctx.session, ctx.tenant_id, appt.resource_id)
            settings = await service.get_settings_row(ctx.session, ctx.tenant_id)
            notice = service.confirmation_block(
                appt, resource, slots.zone(ctx.tenant.timezone), settings
            ).replace("*Appointment confirmed*", "*Appointment moved*", 1)
    if body.status is not None and body.status != appt.status:
        changes["status"] = {"from": appt.status, "to": body.status}
        appt.status = body.status
    if changes:
        await record(
            ctx.session,
            "appointment.updated",
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="appointment",
            target_id=appt.id,
            details=changes,
        )
    return await _result(ctx, appt, notice=notice, notify=body.notify and notice is not None)


@router.post("/{appointment_id}/cancel", response_model=AppointmentResult)
async def cancel_appointment(
    appointment_id: uuid.UUID, body: CancelIn, ctx: Staff, meta: Meta
) -> AppointmentResult:
    """Cancel an appointment and, by default, tell the customer on WhatsApp (possible only
    within 24 hours of their last message)."""
    appt = await service.get_appointment(ctx.session, ctx.tenant_id, appointment_id, lock=True)
    if appt.status != AppointmentStatus.CONFIRMED:
        raise Conflict("Only confirmed appointments can be cancelled.", code="not_confirmed")
    service.cancel(appt, by="staff")
    await record(
        ctx.session,
        "appointment.cancelled",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="appointment",
        target_id=appt.id,
        details={"reference": appt.reference},
    )
    resource = await service.get_resource(ctx.session, ctx.tenant_id, appt.resource_id)
    notice = service.cancelled_block(appt, resource, slots.zone(ctx.tenant.timezone)) + (
        "\n\nPlease message us here if you would like a new time."
    )
    return await _result(ctx, appt, notice=notice, notify=body.notify and appt.starts_at > datetime.now(UTC))
