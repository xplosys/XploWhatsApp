"""Transactional email. SMTP delivery happens in a Celery worker so API
requests never wait on a mail server."""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import structlog

from app.core.config import EmailBackend, get_settings


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    text: str


# Used by the memory backend (tests and local inspection).
OUTBOX: list[OutgoingEmail] = []


def deliver_smtp(email: OutgoingEmail) -> None:
    settings = get_settings()
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = email.to
    msg["Subject"] = email.subject
    msg.set_content(email.text)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_username and settings.smtp_password:
            smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        smtp.send_message(msg)


def send_email(email: OutgoingEmail) -> None:
    backend = get_settings().email_backend
    if backend == EmailBackend.MEMORY:
        OUTBOX.append(email)
    elif backend == EmailBackend.CONSOLE:
        structlog.get_logger("email").info(
            "email_console", to=email.to, subject=email.subject, body=email.text
        )
    else:
        from app.workers.tasks.notifications import send_email_task

        send_email_task.delay(email.to, email.subject, email.text)


def verification_email(to: str, link: str) -> OutgoingEmail:
    return OutgoingEmail(
        to=to,
        subject="Verify your XploWhatsApp email",
        text=f"Welcome to XploWhatsApp.\n\nConfirm your email address by opening this link:\n{link}\n\n"
        "If you did not create an account, you can ignore this email.",
    )


def password_reset_email(to: str, link: str) -> OutgoingEmail:
    return OutgoingEmail(
        to=to,
        subject="Reset your XploWhatsApp password",
        text=f"Someone asked to reset the password for this account.\n\nReset it here (valid for 1 hour):\n"
        f"{link}\n\nIf this was not you, ignore this email; your password stays the same.",
    )


def invitation_email(to: str, tenant_name: str, inviter: str, link: str) -> OutgoingEmail:
    return OutgoingEmail(
        to=to,
        subject=f"You're invited to {tenant_name} on XploWhatsApp",
        text=(
            f"{inviter} invited you to join {tenant_name} on XploWhatsApp.\n\n"
            f"Accept the invitation:\n{link}\n\nThis invitation expires in 7 days."
        ),
    )
