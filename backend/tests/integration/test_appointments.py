"""Appointments: doctors and services, free times, staff bookings, the WhatsApp assistant
booking, moving and cancelling by itself, and reminders, against fake AI and Graph APIs."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select, text, update

from app.db.session import admin_transaction, app_transaction
from app.modules.appointments import messaging
from app.modules.appointments.models import Appointment
from app.modules.conversations.models import Conversation
from app.modules.whatsapp.models import MessageTemplate, WhatsAppAccount
from tests.conftest import Actor, RegisterFn
from tests.integration.test_ai import (  # noqa: F401  (fixtures)
    ai,
    ai_messages,
    clinic,
    config,
    deliver,
    meta,
    models,
    plan_limits,
    run_jobs,
    setup_assistant,
)
from tests.integration.test_inbox import join
from tests.support.ai import FakeAI
from tests.support.meta import FakeMeta, inbound_text

API = "/api/v1"
TZ = ZoneInfo("Asia/Kolkata")
WEEK = {
    d: [["09:00", "13:00"], ["16:00", "18:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}
CUSTOMER = "919812345678"  # the number inbound_text writes from


def tomorrow() -> date:
    return datetime.now(TZ).date() + timedelta(days=1)


async def add_doctor(owner: Actor, **extra: Any) -> dict[str, Any]:
    body = {
        "name": "Dr. Asha Rao",
        "department": "Cardiology",
        "description": "Heart specialist",
        "slot_minutes": 30,
        "weekly_hours": WEEK,
        **extra,
    }
    r = await owner.post(f"{API}/appointments/resources", json=body)
    assert r.status_code == 201, r.text
    return dict(r.json())


async def enable_booking(owner: Actor, **extra: Any) -> dict[str, Any]:
    body = {
        "ai_booking_enabled": True,
        "min_notice_minutes": 0,
        "confirmation_note": "Please arrive 15 minutes early.",
        **extra,
    }
    r = await owner.put(f"{API}/appointments/settings", json=body)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def appointments(tenant_id: str) -> list[Appointment]:
    async with app_transaction(uuid.UUID(tenant_id)) as s:
        rows = list((await s.execute(select(Appointment).order_by(Appointment.created_at))).scalars())
        for r in rows:
            s.expunge(r)
        return rows


async def staff_book(
    owner: Actor, doctor: dict[str, Any], *, phone: str, at: str = "10:00", **extra: Any
) -> httpx.Response:
    return await owner.post(
        f"{API}/appointments",
        json={
            "resource_id": doctor["id"],
            "phone": phone,
            "date": tomorrow().isoformat(),
            "time": at,
            "patient_name": "Someone Else",
            **extra,
        },
    )


@pytest.fixture
async def hospital(clinic: Actor, ai: FakeAI) -> tuple[Actor, dict[str, Any]]:
    await setup_assistant(clinic, config())
    doctor = await add_doctor(clinic)
    await enable_booking(clinic)
    return clinic, doctor


# ---- set-up and permissions -----------------------------------------------------


async def test_doctors_hours_settings_and_permissions(
    register: RegisterFn, clinic: Actor, plan_limits: Any
) -> None:
    bad = await clinic.post(
        f"{API}/appointments/resources",
        json={"name": "Dr X", "weekly_hours": {"mon": [["13:00", "09:00"]]}},
    )
    assert bad.status_code == 422 and "must be after" in bad.text
    overlap = await clinic.post(
        f"{API}/appointments/resources",
        json={"name": "Dr X", "weekly_hours": {"mon": [["09:00", "12:00"], ["11:00", "13:00"]]}},
    )
    assert overlap.status_code == 422 and "overlap" in overlap.text
    doctor = await add_doctor(clinic, weekly_hours={"tue": [["16:00", "18:00"]], "mon": [["9:00", "13:00"]]})
    assert doctor["weekly_hours"] == {"mon": [["09:00", "13:00"]], "tue": [["16:00", "18:00"]]}

    settings = (await clinic.get(f"{API}/appointments/settings")).json()
    assert settings["ai_booking_enabled"] is False and settings["timezone"] == "Asia/Kolkata"
    settings = await enable_booking(clinic, horizon_days=30)
    assert settings["horizon_days"] == 30 and settings["active_resources"] == 1

    await plan_limits(max_users=10)
    agent = await join(register, clinic, "agent@sunrisedental.in", "agent")
    assert (await agent.get(f"{API}/appointments/resources")).status_code == 200
    assert (await agent.post(f"{API}/appointments/resources", json={"name": "Dr Y"})).status_code == 403
    assert (await agent.put(f"{API}/appointments/settings", json={})).status_code == 403

    # A doctor who was never booked can be deleted; agents can book and cancel.
    assert (await staff_book(agent, doctor, phone="+919800000001", at="09:00")).status_code in (201, 409)
    spare = await add_doctor(clinic, name="Dr. Spare")
    assert (await clinic.delete(f"{API}/appointments/resources/{spare['id']}")).status_code == 204
    off = dict(doctor, active=False)
    off.pop("id"), off.pop("upcoming"), off.pop("created_at")
    r = await clinic.put(f"{API}/appointments/resources/{doctor['id']}", json=off)
    assert r.status_code == 200 and r.json()["active"] is False


async def test_free_times_skip_booked_slots_days_off_and_full_capacity(clinic: Actor) -> None:
    doctor = await add_doctor(clinic)
    day = tomorrow().isoformat()
    r = await staff_book(clinic, doctor, phone="+919800000001")
    assert r.status_code == 201, r.text
    booked = r.json()
    assert (
        booked["local_start"] == f"{day}T10:00"
        and booked["source"] == "staff"
        and len(booked["reference"]) == 6
    )

    free = (await clinic.get(f"{API}/appointments/availability", params={"start": day, "days": 2})).json()
    [mine] = free
    times = mine["days"][0]["times"]
    assert "10:00" not in times and "09:30" in times and "10:30" in times and "12:30" in times
    assert "13:00" not in times and "16:00" in times

    # The slot is full: staff need allow_overbook to double-book.
    again = await staff_book(clinic, doctor, phone="+919800000002")
    assert again.status_code == 409 and again.json()["error"]["code"] == "slot_taken"
    assert (await staff_book(clinic, doctor, phone="+919800000002", allow_overbook=True)).status_code == 201

    # The same person at the same time is not booked twice.
    dup = await staff_book(clinic, doctor, phone="+919800000001")
    assert dup.status_code == 201 and dup.json()["id"] == booked["id"]

    off = await clinic.post(
        f"{API}/appointments/time-off",
        json={"resource_id": doctor["id"], "starts_on": day, "ends_on": day, "reason": "Conference"},
    )
    assert off.status_code == 201
    free = (await clinic.get(f"{API}/appointments/availability", params={"start": day, "days": 2})).json()
    assert free[0]["days"][0]["times"] == [] and free[0]["days"][1]["times"]

    listed = (await clinic.get(f"{API}/appointments", params={"start": day, "end": day})).json()
    assert listed["timezone"] == "Asia/Kolkata" and len(listed["items"]) == 2
    assert (await clinic.delete(f"{API}/appointments/resources/{doctor['id']}")).status_code == 409


async def test_staff_move_complete_and_cancel(clinic: Actor) -> None:
    doctor = await add_doctor(clinic)
    appt = (await staff_book(clinic, doctor, phone="+919800000001")).json()
    moved = await clinic.patch(
        f"{API}/appointments/{appt['id']}", json={"date": tomorrow().isoformat(), "time": "11:30"}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["local_start"].endswith("T11:30") and moved.json()["reference"] == appt["reference"]
    # The customer never wrote to us, so they can't be told by free text.
    cancelled = await clinic.post(f"{API}/appointments/{appt['id']}/cancel", json={"notify": True})
    assert cancelled.status_code == 200
    body = cancelled.json()
    assert (
        body["status"] == "cancelled"
        and body["notified"] is False
        and body["notify_error"] == "window_closed"
    )
    again = await clinic.patch(f"{API}/appointments/{appt['id']}", json={"status": "completed"})
    assert again.status_code == 409


# ---- the assistant books ----------------------------------------------------------


async def test_assistant_books_a_confirmed_appointment(
    hospital: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, ai: FakeAI, meta: FakeMeta
) -> None:
    owner, _ = hospital
    day = tomorrow()
    ai.reply("openai", "Dr. Asha Rao has free times tomorrow from 09:00. Which time suits you?")
    assert (await deliver(client, inbound_text("Can you please book an appointment for me?")))[1] == "replied"
    prompt = ai.calls("openai")[-1]["instructions"]
    assert "APPOINTMENTS" in prompt and "[R1] Dr. Asha Rao - department: Cardiology" in prompt
    assert f"({day.isoformat()}): 09:00-12:30, 16:00-17:30" in prompt
    assert (
        "bookings" not in prompt.split("RULES FOR EVERY REPLY")[1].split("\n")[3]
    )  # rule 3 no longer hands bookings over

    ai.reply(
        "openai",
        "Done! Your appointment is booked.",
        booking={
            "action": "book",
            "resource": "R1",
            "date": day.isoformat(),
            "time": "10:30",
            "patient_name": "Ramesh Kumar",
            "notes": "Chest pain",
        },
    )
    results = await deliver(client, inbound_text("Yes, 10:30 for Ramesh Kumar", wamid="wamid.IN0002"))
    assert results == ["processed", "booking:booked", "done"]

    [appt] = await appointments(owner.tenant_id)
    assert appt.status == "confirmed" and appt.source == "ai" and appt.patient_name == "Ramesh Kumar"
    assert appt.starts_at == datetime.combine(day, datetime.min.time().replace(hour=10, minute=30), tzinfo=TZ)
    assert appt.notes == "Chest pain" and appt.conversation_id is not None

    message = (await ai_messages(owner.tenant_id))[-1]
    assert message.body.startswith("Done! Your appointment is booked.\n\n*Appointment confirmed*")
    assert f"Reference: *{appt.reference}*" in message.body and "10:30 AM" in message.body
    assert "Please arrive 15 minutes early." in message.body
    assert message.ai_provenance["booking"]["result"] == "booked"
    assert meta.sent[-1]["text"]["body"] == message.body

    # The next reply knows about it, so the customer can cancel or move it.
    ai.reply("openai", "You're welcome!")
    await deliver(client, inbound_text("Thanks", wamid="wamid.IN0003"))
    assert f"Reference {appt.reference}: Dr. Asha Rao [R1]" in ai.calls("openai")[-1]["instructions"]

    listed = (
        await owner.get(f"{API}/appointments", params={"start": day.isoformat(), "end": day.isoformat()})
    ).json()
    assert listed["items"][0]["contact"]["phone_e164"] == "+" + CUSTOMER


async def test_taken_time_is_never_booked_and_free_times_are_offered(
    hospital: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, ai: FakeAI
) -> None:
    owner, doctor = hospital
    assert (await staff_book(owner, doctor, phone="+919800000009")).status_code == 201
    ai.reply(
        "openai",
        "Booked!",
        booking={
            "action": "book",
            "resource": "R1",
            "date": tomorrow().isoformat(),
            "time": "10:00",
            "patient_name": "Priya",
        },
    )
    results = await deliver(client, inbound_text("Book 10am tomorrow please"))
    assert results[1] == "booking:slot_taken"
    body = (await ai_messages(owner.tenant_id))[-1].body
    assert "Booked!" not in body and "has just been taken" in body
    assert "9:00 AM, 9:30 AM, 10:30 AM" in body and "Which time would you like?" in body
    assert len(await appointments(owner.tenant_id)) == 1

    # Outside the doctor's hours, or an unknown doctor code, is refused too.
    ai.reply(
        "openai",
        "Booked!",
        booking={"action": "book", "resource": "R1", "date": tomorrow().isoformat(), "time": "14:00"},
    )
    assert (await deliver(client, inbound_text("2pm then", wamid="wamid.IN0002")))[
        1
    ] == "booking:outside_hours"
    ai.reply(
        "openai",
        "Booked!",
        booking={"action": "book", "resource": "R9", "date": tomorrow().isoformat(), "time": "09:00"},
    )
    assert (await deliver(client, inbound_text("9am", wamid="wamid.IN0003")))[1] == "booking:unclear"
    assert len(await appointments(owner.tenant_id)) == 1


async def test_assistant_moves_and_cancels_by_reference(
    hospital: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, ai: FakeAI
) -> None:
    owner, doctor = hospital
    day = tomorrow().isoformat()
    ai.reply(
        "openai",
        "Done.",
        booking={"action": "book", "resource": "R1", "date": day, "time": "09:00", "patient_name": "Priya"},
    )
    await deliver(client, inbound_text("Book 9am"))
    [appt] = await appointments(owner.tenant_id)

    ai.reply(
        "openai",
        "Moved.",
        booking={"action": "reschedule", "reference": appt.reference.lower(), "date": day, "time": "16:30"},
    )
    assert (await deliver(client, inbound_text("Move it to 4:30pm", wamid="wamid.IN0002")))[
        1
    ] == "booking:rescheduled"
    [moved] = await appointments(owner.tenant_id)
    assert (
        moved.id == appt.id
        and moved.starts_at.astimezone(TZ).strftime("%H:%M") == "16:30"
        and moved.times_moved == 1
    )
    body = (await ai_messages(owner.tenant_id))[-1].body
    assert "*Appointment moved*" in body and "9:00 AM, is no longer booked" in body

    # Someone else's appointment can't be touched with its reference.
    other = (await staff_book(owner, doctor, phone="+919800000009", at="12:00")).json()
    ai.reply("openai", "Cancelled.", booking={"action": "cancel", "reference": other["reference"]})
    assert (await deliver(client, inbound_text(f"Cancel {other['reference']}", wamid="wamid.IN0003")))[
        1
    ] == "booking:not_found"

    ai.reply("openai", "Cancelled.", booking={"action": "cancel", "reference": appt.reference})
    assert (await deliver(client, inbound_text("Cancel mine", wamid="wamid.IN0004")))[
        1
    ] == "booking:cancelled"
    statuses = {a.reference: (a.status, a.cancelled_by) for a in await appointments(owner.tenant_id)}
    assert statuses == {appt.reference: ("cancelled", "customer"), other["reference"]: ("confirmed", None)}
    assert "*Appointment cancelled*" in (await ai_messages(owner.tenant_id))[-1].body


async def test_limit_on_upcoming_appointments_per_number(
    hospital: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, ai: FakeAI
) -> None:
    owner, _ = hospital
    await enable_booking(owner, max_upcoming_per_contact=1)
    day = tomorrow().isoformat()
    ai.reply(
        "openai",
        "Done.",
        booking={"action": "book", "resource": "R1", "date": day, "time": "09:00", "patient_name": "A"},
    )
    await deliver(client, inbound_text("Book 9"))
    ai.reply(
        "openai",
        "Done.",
        booking={"action": "book", "resource": "R1", "date": day, "time": "09:30", "patient_name": "B"},
    )
    assert (await deliver(client, inbound_text("And 9:30", wamid="wamid.IN0002")))[
        1
    ] == "booking:too_many_upcoming"
    assert len(await appointments(owner.tenant_id)) == 1


async def test_no_booking_when_the_business_has_not_turned_it_on(
    clinic: Actor, client: httpx.AsyncClient, ai: FakeAI
) -> None:
    await setup_assistant(clinic, config())
    await add_doctor(clinic)  # booking settings never turned on
    ai.reply(
        "openai",
        "Sure!",
        booking={"action": "book", "resource": "R1", "date": tomorrow().isoformat(), "time": "09:00"},
    )
    assert (await deliver(client, inbound_text("Book 9am")))[1] == "replied"
    assert await appointments(clinic.tenant_id) == []
    prompt = ai.calls("openai")[-1]["instructions"]
    assert "APPOINTMENTS" not in prompt and "bookings, cancellations" in prompt


async def test_preview_shows_the_booking_without_making_it(
    hospital: tuple[Actor, dict[str, Any]], ai: FakeAI
) -> None:
    owner, _ = hospital
    ai.reply(
        "openai",
        "Done.",
        booking={"action": "book", "resource": "R1", "date": tomorrow().isoformat(), "time": "09:00"},
    )
    r = await owner.post(
        f"{API}/ai/assistant/preview", json={"messages": [{"role": "user", "text": "Book 9am"}]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["booking"]["action"] == "book" and await appointments(owner.tenant_id) == []


# ---- reminders ----------------------------------------------------------------------


async def _age(tenant_id: str, days: int = 3) -> None:
    """Make every appointment look booked a few days ago, so its reminder is due."""
    async with admin_transaction() as s:
        await s.execute(
            text(
                "UPDATE appointments SET created_at = now() - make_interval(days => :d) WHERE tenant_id = :t"
            ),
            {"d": days, "t": uuid.UUID(tenant_id)},
        )


async def _close_window(tenant_id: str) -> None:
    async with app_transaction(uuid.UUID(tenant_id)) as s:
        await s.execute(
            update(Conversation).values(service_window_expires_at=datetime.now(UTC) - timedelta(hours=1))
        )


async def test_reminders_by_text_by_template_or_skipped(
    hospital: tuple[Actor, dict[str, Any]], client: httpx.AsyncClient, ai: FakeAI, meta: FakeMeta
) -> None:
    owner, doctor = hospital
    await enable_booking(owner, reminders_enabled=True, reminder_hours_before=48)
    day = tomorrow().isoformat()
    ai.reply(
        "openai",
        "Done.",
        booking={"action": "book", "resource": "R1", "date": day, "time": "09:00", "patient_name": "Priya"},
    )
    await deliver(client, inbound_text("Book 9am"))

    # Booked inside the reminder window: the confirmation is enough.
    assert (await messaging.sweep())["due"] == 0
    await _age(owner.tenant_id)

    # The customer wrote recently, so a plain text reminder goes out.
    assert await messaging.sweep() == {"due": 1, "sent": 1}
    await run_jobs()
    assert (
        "this is a reminder of your appointment with Dr. Asha Rao (Cardiology)"
        in meta.sent[-1]["text"]["body"]
    )
    assert await messaging.sweep() == {"due": 0}  # never twice

    # Outside the 24-hour window without a template: skipped, and the calendar says why.
    assert (
        await staff_book(owner, doctor, phone="+" + CUSTOMER, at="12:00", patient_name="Priya")
    ).status_code == 201
    await _age(owner.tenant_id)
    await _close_window(owner.tenant_id)
    assert await messaging.sweep() == {"due": 1, "skipped": 1}
    rows = {a.starts_at.astimezone(TZ).strftime("%H:%M"): a for a in await appointments(owner.tenant_id)}
    assert rows["12:00"].reminder_status == "skipped" and rows["12:00"].reminder_detail == "no_template"

    # With an approved template, its variables are filled in order.
    async with app_transaction(uuid.UUID(owner.tenant_id)) as s:
        account = (await s.execute(select(WhatsAppAccount))).scalar_one()
        s.add(
            MessageTemplate(
                tenant_id=account.tenant_id,
                account_id=account.id,
                name="appointment_reminder",
                language="en",
                category="UTILITY",
                status="APPROVED",
                components=[{"type": "BODY", "text": "Hi {{1}}, see you with {{2}} on {{3}} at {{4}}."}],
            )
        )
    await enable_booking(
        owner,
        reminders_enabled=True,
        reminder_hours_before=48,
        reminder_template_name="appointment_reminder",
        reminder_template_language="en",
    )
    assert (
        await staff_book(owner, doctor, phone="+" + CUSTOMER, at="12:30", patient_name="Priya")
    ).status_code == 201
    await _age(owner.tenant_id)
    assert await messaging.sweep() == {"due": 1, "sent": 1}
    await run_jobs()
    sent = meta.sent[-1]
    assert sent["type"] == "template" and sent["template"]["name"] == "appointment_reminder"
    params = [p["text"] for p in sent["template"]["components"][0]["parameters"]]
    assert params[0] == "Priya" and params[1] == "Dr. Asha Rao (Cardiology)" and params[3] == "12:30 PM"
