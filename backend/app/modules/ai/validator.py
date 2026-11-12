"""Checks every AI answer before it can reach a customer.

An answer that fails is never sent: the orchestrator tries the next model, and if
none passes, the conversation is handed to a person with the business's handover
message.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from app.modules.ai.prompt import PROMPT_MARKERS

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bEAA[A-Za-z0-9]{30,}"),  # Meta access tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

CURRENCY = r"(?:₹|rs\.?|inr|\$|usd|£|gbp|€|eur|aed|sgd)"
NUMBER = r"\d[\d,]*(?:\.\d+)?"
MONEY_RE = re.compile(
    rf"(?:{CURRENCY}\s*({NUMBER}))|(?:({NUMBER})\s*(?:{CURRENCY}|rupees?|dollars?|pounds?|euros?))", re.I
)
PERCENT_RE = re.compile(rf"({NUMBER})\s*%")
TIME_RE = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)", re.I)
CLOCK_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
PHONE_RE = re.compile(r"(?:\+?\d[\d\s()-]{7,}\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s)>\]]+", re.I)


@dataclass
class Verdict:
    kind: Literal["reply", "handover", "invalid"]
    text: str = ""
    out_of_scope: bool = False
    issues: list[str] = field(default_factory=list)


def _parse(raw: str) -> dict[str, object] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _norm_number(value: str) -> str:
    return value.replace(",", "").rstrip("0").rstrip(".") if "." in value else value.replace(",", "")


def _times(text: str) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    for hour, minute, half in TIME_RE.findall(text):
        h = int(hour) % 12 + (12 if half.lower().startswith("p") else 0)
        found.add((h, int(minute or 0)))
    for hour, minute in CLOCK_RE.findall(text):
        h = int(hour)
        if h < 24:
            found.add((h, int(minute)))
            if h <= 12:
                found.add(((h % 12) + 12, int(minute)))  # "5:30" may mean 17:30
    return found


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def ungrounded_facts(reply: str, grounding: str) -> list[str]:
    """Prices, percentages, times, phone numbers, emails and links in the reply that do
    not appear in the grounding text."""
    problems: list[str] = []
    known_numbers = {_norm_number(n) for n in re.findall(NUMBER, grounding)}
    for a, b in MONEY_RE.findall(reply):
        value = _norm_number(a or b)
        if value not in known_numbers:
            problems.append(f"price {a or b}")
    for value in PERCENT_RE.findall(reply):
        if _norm_number(value) not in known_numbers:
            problems.append(f"percentage {value}%")
    known_times = _times(grounding)
    for hour, minute, half in TIME_RE.findall(reply):
        h = int(hour) % 12 + (12 if half.lower().startswith("p") else 0)
        if (h, int(minute or 0)) not in known_times:
            problems.append(f"time {hour}{':' + minute if minute else ''}{half}")
    for hour, minute in CLOCK_RE.findall(reply):
        if int(hour) < 24 and (int(hour), int(minute)) not in known_times:
            problems.append(f"time {hour}:{minute}")
    grounding_digits = _digits(grounding)
    for phone in PHONE_RE.findall(reply):
        digits = _digits(phone)
        if len(digits) >= 8 and digits[-8:] not in grounding_digits:
            problems.append(f"phone {phone.strip()}")
    lowered = grounding.lower()
    for email in EMAIL_RE.findall(reply):
        if email.lower() not in lowered:
            problems.append(f"email {email}")
    for url in URL_RE.findall(reply):
        bare = re.sub(r"^https?://", "", url.lower()).rstrip("/.,")
        if bare not in lowered:
            problems.append(f"link {url}")
    return sorted(set(problems))


def validate(raw: str, *, max_chars: int, grounding: str) -> Verdict:
    data = _parse(raw)
    if data is None:
        return Verdict("invalid", issues=["not_json"])
    reply = data.get("reply")
    needs_human = data.get("needs_human") is True
    out_of_scope = data.get("out_of_scope") is True
    if not isinstance(reply, str):
        return Verdict("invalid", issues=["missing_reply"])
    reply = reply.strip()
    if not reply:
        return (
            Verdict("handover", issues=["empty_reply"])
            if needs_human
            else Verdict("invalid", issues=["empty_reply"])
        )
    if len(reply) > max_chars:
        return Verdict("invalid", issues=["too_long"])
    if any(p.search(reply) for p in SECRET_PATTERNS):
        return Verdict("invalid", issues=["secret_in_reply"])
    if any(marker.lower() in reply.lower() for marker in PROMPT_MARKERS):
        return Verdict("invalid", issues=["prompt_leak"])
    facts = ungrounded_facts(reply, grounding)
    if facts:
        return Verdict("invalid", issues=[f"ungrounded: {', '.join(facts)}"])
    return Verdict("handover" if needs_human else "reply", text=reply, out_of_scope=out_of_scope)
