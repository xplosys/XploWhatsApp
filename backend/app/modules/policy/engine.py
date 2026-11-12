"""Messaging policy engine v1.

Every outbound message goes through `evaluate()` twice: when it is queued (so
the caller gets an immediate, specific answer) and again just before it is sent
(the service window may have closed in between). The rules follow Meta's
WhatsApp Business Messaging Policy; thresholds are configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.modules.conversations.models import ConsentStatus, Contact, Conversation
from app.modules.whatsapp.models import (
    AccountStatus,
    MessageTemplate,
    NumberStatus,
    WhatsAppAccount,
    WhatsAppNumber,
)


class MessageKind(StrEnum):
    FREE_FORM = "free_form"  # text, media, interactive: only inside the service window
    TEMPLATE = "template"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str
    message: str
    requires_template: bool = False

    @classmethod
    def allow(cls) -> Decision:
        return cls(True, "allowed", "Message can be sent.")


def _deny(code: str, message: str, requires_template: bool = False) -> Decision:
    return Decision(False, code, message, requires_template)


def service_window_open(conversation: Conversation | None, now: datetime) -> bool:
    return bool(
        conversation
        and conversation.service_window_expires_at
        and conversation.service_window_expires_at > now
    )


def evaluate(
    *,
    account: WhatsAppAccount,
    number: WhatsAppNumber,
    contact: Contact,
    conversation: Conversation | None,
    kind: MessageKind,
    now: datetime,
    template: MessageTemplate | None = None,
    template_language: str | None = None,
) -> Decision:
    if account.status == AccountStatus.NEEDS_RECONNECT:
        return _deny("account_needs_reconnect", "Reconnect this WhatsApp account before sending.")
    if account.status != AccountStatus.CONNECTED:
        return _deny("account_not_connected", "This WhatsApp account is not connected.")
    if number.status != NumberStatus.CONNECTED:
        return _deny("number_not_connected", "This phone number is not registered for sending yet.")
    if number.account_id != account.id:
        return _deny("number_account_mismatch", "The number does not belong to this account.")

    if contact.consent_status == ConsentStatus.OPTED_OUT:
        return _deny("contact_opted_out", "This contact has opted out of messages from your business.")
    if not contact.wa_id and not contact.bsuid:
        return _deny("contact_unreachable", "This contact has no WhatsApp identifier.")

    if kind == MessageKind.FREE_FORM:
        if not service_window_open(conversation, now):
            return _deny(
                "outside_service_window",
                "More than 24 hours have passed since the customer's last message. "
                "Use an approved template to start the conversation again.",
                requires_template=True,
            )
        return Decision.allow()

    # Template messages
    if template is None:
        return _deny(
            "template_not_found", "This template does not exist on the account. Sync templates and retry."
        )
    if template.account_id != account.id:
        return _deny("template_account_mismatch", "This template belongs to a different WhatsApp account.")
    if template.status != "APPROVED":
        return _deny("template_not_approved", f"This template is {template.status.lower()}, not approved.")
    if template_language and template.language != template_language:
        return _deny("template_language_unavailable", "This template is not approved in that language.")
    category = (template.category or "").upper()
    if category == "MARKETING":
        if contact.consent_status != ConsentStatus.OPTED_IN:
            return _deny(
                "marketing_opt_in_required",
                "Marketing templates need the contact's recorded opt-in.",
            )
        if (number.quality_rating or "").upper() == "RED":
            return _deny(
                "number_quality_low",
                "Marketing is paused while this number's quality rating is low.",
            )
    if category == "AUTHENTICATION" and not contact.wa_id:
        return _deny(
            "authentication_needs_phone",
            "Authentication templates can only be sent to a phone number, not a username.",
        )
    return Decision.allow()
