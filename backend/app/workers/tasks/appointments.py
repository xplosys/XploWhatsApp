"""Appointment tasks (queue: default): reminders before appointments."""

from __future__ import annotations

from typing import Any

from app.workers.celery_app import celery_app
from app.workers.runtime import run_async


@celery_app.task(name="app.workers.tasks.appointments.send_reminders")
def send_reminders() -> dict[str, Any]:
    from app.modules.appointments.messaging import sweep

    return dict(run_async(sweep()))
