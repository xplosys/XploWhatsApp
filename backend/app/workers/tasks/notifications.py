from __future__ import annotations

import smtplib

from app.modules.notifications.email import OutgoingEmail, deliver_smtp
from app.workers.celery_app import celery_app


@celery_app.task(
    name="app.workers.tasks.notifications.send_email_task",
    autoretry_for=(smtplib.SMTPException, OSError),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=8,
)
def send_email_task(to: str, subject: str, text: str) -> None:
    deliver_smtp(OutgoingEmail(to=to, subject=subject, text=text))
