from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.modules.conversations.models import ConsentStatus, Contact, Conversation, Message
from app.modules.conversations.service import apply_status, normalise_wa_id
from app.modules.policy.engine import MessageKind, evaluate
from app.modules.webhooks.ingest import dedup_key
from app.modules.webhooks.processor import summarise
from app.modules.webhooks.signature import compute_signature, is_valid_signature
from app.modules.whatsapp.graph import _parse_error
from app.modules.whatsapp.models import MessageTemplate, WhatsAppAccount, WhatsAppNumber

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _world(**overrides: object) -> dict[str, object]:
    account = WhatsAppAccount(id=uuid.uuid4(), status="connected")
    number = WhatsAppNumber(
        id=uuid.uuid4(), account_id=account.id, status="connected", quality_rating="GREEN"
    )
    contact = Contact(id=uuid.uuid4(), wa_id="919812345678", consent_status=ConsentStatus.UNKNOWN)
    conversation = Conversation(service_window_expires_at=NOW + timedelta(hours=2))
    world: dict[str, object] = {
        "account": account,
        "number": number,
        "contact": contact,
        "conversation": conversation,
        "kind": MessageKind.FREE_FORM,
        "now": NOW,
    }
    world.update(overrides)
    return world


def _template(
    account: WhatsAppAccount, category: str = "UTILITY", status: str = "APPROVED"
) -> MessageTemplate:
    return MessageTemplate(account_id=account.id, name="t", language="en", category=category, status=status)


def test_free_form_allowed_only_inside_service_window() -> None:
    assert evaluate(**_world()).allowed  # type: ignore[arg-type]
    closed = Conversation(service_window_expires_at=NOW - timedelta(seconds=1))
    decision = evaluate(**_world(conversation=closed))  # type: ignore[arg-type]
    assert not decision.allowed and decision.code == "outside_service_window" and decision.requires_template
    assert evaluate(**_world(conversation=None)).code == "outside_service_window"  # type: ignore[arg-type]


def test_opt_out_blocks_everything() -> None:
    w = _world()
    w["contact"].consent_status = ConsentStatus.OPTED_OUT  # type: ignore[attr-defined]
    assert evaluate(**w).code == "contact_opted_out"  # type: ignore[arg-type]
    w["kind"] = MessageKind.TEMPLATE
    w["template"] = _template(w["account"])  # type: ignore[arg-type]
    assert evaluate(**w).code == "contact_opted_out"  # type: ignore[arg-type]


def test_account_and_number_state() -> None:
    w = _world()
    w["account"].status = "needs_reconnect"  # type: ignore[attr-defined]
    assert evaluate(**w).code == "account_needs_reconnect"  # type: ignore[arg-type]
    w = _world()
    w["number"].status = "pending"  # type: ignore[attr-defined]
    assert evaluate(**w).code == "number_not_connected"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("category", "status", "consent", "quality", "expected"),
    [
        ("UTILITY", "APPROVED", ConsentStatus.UNKNOWN, "GREEN", "allowed"),
        ("UTILITY", "REJECTED", ConsentStatus.UNKNOWN, "GREEN", "template_not_approved"),
        ("MARKETING", "APPROVED", ConsentStatus.UNKNOWN, "GREEN", "marketing_opt_in_required"),
        ("MARKETING", "APPROVED", ConsentStatus.OPTED_IN, "GREEN", "allowed"),
        ("MARKETING", "APPROVED", ConsentStatus.OPTED_IN, "RED", "number_quality_low"),
        ("UTILITY", "APPROVED", ConsentStatus.UNKNOWN, "RED", "allowed"),
    ],
)
def test_template_rules(category: str, status: str, consent: str, quality: str, expected: str) -> None:
    closed = Conversation(service_window_expires_at=None)  # templates work outside the window
    w = _world(conversation=closed, kind=MessageKind.TEMPLATE)
    w["contact"].consent_status = consent  # type: ignore[attr-defined]
    w["number"].quality_rating = quality  # type: ignore[attr-defined]
    w["template"] = _template(w["account"], category, status)  # type: ignore[arg-type]
    assert evaluate(**w).code == expected  # type: ignore[arg-type]


def test_authentication_template_needs_phone() -> None:
    w = _world(kind=MessageKind.TEMPLATE)
    w["contact"] = Contact(id=uuid.uuid4(), wa_id=None, bsuid="IN.123", consent_status=ConsentStatus.UNKNOWN)
    w["template"] = _template(w["account"], "AUTHENTICATION")  # type: ignore[arg-type]
    assert evaluate(**w).code == "authentication_needs_phone"  # type: ignore[arg-type]


def test_signature_check() -> None:
    body = b'{"object":"whatsapp_business_account"}'
    good = compute_signature(body, "s3cret")
    assert is_valid_signature(body, good, "s3cret")
    assert not is_valid_signature(body, good, "other")
    assert not is_valid_signature(body + b" ", good, "s3cret")
    assert not is_valid_signature(body, None, "s3cret")
    assert not is_valid_signature(body, good.replace("sha256=", "sha1="), "s3cret")


def test_dedup_key_is_stable_and_order_independent() -> None:
    a = dedup_key("messages", "1", {"x": 1, "y": [1, 2]})
    assert a == dedup_key("messages", "1", {"y": [1, 2], "x": 1})
    assert a != dedup_key("statuses", "1", {"x": 1, "y": [1, 2]})


def test_status_never_moves_backwards() -> None:
    msg = Message(status="accepted")
    assert apply_status(msg, status="read", at=NOW)
    assert not apply_status(msg, status="delivered", at=NOW)
    assert msg.status == "read"
    assert apply_status(msg, status="failed", at=NOW, errors=[{"code": 131049, "title": "Not delivered"}])
    assert msg.status == "failed" and msg.error_code == "131049"
    assert not apply_status(msg, status="sent", at=NOW)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"type": "text", "text": {"body": "hi"}}, ("text", "hi")),
        ({"type": "image", "image": {"id": "m1", "caption": "x-ray"}}, ("image", "x-ray")),
        ({"type": "button", "button": {"text": "Confirm", "payload": "c"}}, ("button", "Confirm")),
        (
            {
                "type": "interactive",
                "interactive": {"type": "list_reply", "list_reply": {"id": "1", "title": "Mon 10am"}},
            },
            ("interactive", "Mon 10am"),
        ),
        ({"type": "reaction", "reaction": {"message_id": "w", "emoji": "👍"}}, ("reaction", "👍")),
        ({"type": "unsupported", "errors": [{"code": 131051}]}, ("unsupported", None)),
    ],
)
def test_inbound_summaries(message: dict[str, object], expected: tuple[str, str | None]) -> None:
    mtype, body, _ = summarise(message)
    assert (mtype, body) == expected


def test_graph_error_mapping() -> None:
    def resp(status: int, code: int) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "m", "code": code, "fbtrace_id": "T"}})

    assert _parse_error(resp(401, 190)).is_token_error
    assert _parse_error(resp(400, 130429)).retryable  # rate limit
    assert _parse_error(resp(500, 1)).retryable
    assert not _parse_error(resp(400, 131026)).retryable
    assert _parse_error(resp(400, 100)).public()["fbtrace_id"] == "T"


def test_normalise_wa_id() -> None:
    assert normalise_wa_id("+91 98123-45678") == "919812345678"
    assert normalise_wa_id("") is None


def test_template_preview_fills_body_values() -> None:
    from app.modules.conversations.outbound import template_preview

    t = MessageTemplate(
        name="r", language="en", components=[{"type": "BODY", "text": "Hi {{1}}, see you {{2}}."}]
    )
    filled = [
        {"type": "body", "parameters": [{"type": "text", "text": "Priya"}, {"type": "text", "text": "Mon"}]}
    ]
    assert template_preview(t, filled) == "Hi Priya, see you Mon."
    assert template_preview(t) == "Hi {{1}}, see you {{2}}."
    assert template_preview(MessageTemplate(name="x", language="en", components=[])) == "Template: x"
