from __future__ import annotations

import json

import pytest

from app.modules.ai.assistant import AssistantConfig
from app.modules.ai.prompt import REPLY_SCHEMA, system_prompt, trim_history
from app.modules.ai.providers.base import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderBillingError,
    ProviderRateLimited,
    ProviderUnavailable,
    Turn,
    merge_turns,
    raise_for_status,
)
from app.modules.ai.providers.gemini import to_gemini_schema
from app.modules.ai.validator import ungrounded_facts, validate

GROUND = "Cleaning ₹4,500. Open 9am-7:30 pm. Call +91 98765 43210, help@sunrise.in, www.sunrise.in/book"


def _reply(text: str, **flags: bool) -> str:
    return json.dumps({"reply": text, "needs_human": False, "out_of_scope": False, **flags})


@pytest.mark.parametrize(
    "reply",
    [
        "A cleaning is Rs 4500.",
        "It costs ₹4,500.",
        "We open at 9 AM and close at 19:30.",
        "Call 98765 43210 or mail help@sunrise.in",
        "Book at https://www.sunrise.in/book",
        "We are open on weekdays.",
    ],
)
def test_grounded_facts_pass(reply: str) -> None:
    assert ungrounded_facts(reply, GROUND) == []


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        ("A cleaning is ₹500.", "price"),
        ("10% off this week", "percentage"),
        ("We open at 8am", "time"),
        ("Call +91 90000 11111", "phone"),
        ("Mail us at info@other.com", "email"),
        ("See https://evil.example/pay", "link"),
    ],
)
def test_invented_facts_fail(reply: str, problem: str) -> None:
    assert any(p.startswith(problem) for p in ungrounded_facts(reply, GROUND))


def test_validate_shapes() -> None:
    ok = validate(f"```json\n{_reply('Hello!')}\n```", max_chars=100, grounding="")
    assert ok.kind == "reply" and ok.text == "Hello!"
    assert validate("hello", max_chars=100, grounding="").issues == ["not_json"]
    assert validate(_reply("x" * 101), max_chars=100, grounding="").issues == ["too_long"]
    assert validate(_reply("key sk-abcdefghijklmnopqrstu"), max_chars=100, grounding="").issues == [
        "secret_in_reply"
    ]
    assert validate(_reply(""), max_chars=100, grounding="").kind == "invalid"
    assert validate(_reply("", needs_human=True), max_chars=100, grounding="").kind == "handover"
    assert validate(_reply("Let me get a colleague", needs_human=True), max_chars=100, grounding="").text
    assert validate(json.dumps({"reply": 5}), max_chars=100, grounding="").issues == ["missing_reply"]


def test_history_trimming_and_merging() -> None:
    turns = [
        Turn("assistant", "Welcome"),
        Turn("user", "a" * 50),
        Turn("assistant", "b" * 50),
        Turn("user", "c"),
    ]
    assert trim_history(turns, 60) == [Turn("user", "c")]
    assert trim_history(turns, 1000)[0].role == "user"  # never starts with the assistant
    assert merge_turns([Turn("user", "Hi"), Turn("user", "there")]) == [Turn("user", "Hi\nthere")]


def test_prompt_and_schema() -> None:
    cfg = AssistantConfig.model_validate(
        {
            "instructions": "Cleaning ₹800",
            "reply_language": "business_default",
            "primary": {"provider": "openai", "model": "m"},
        }
    )
    prompt = system_prompt(cfg, business_name="Sunrise", default_language="hi")
    assert "Reply in Hindi." in prompt and "Cleaning ₹800" in prompt and "json" in prompt.lower()
    gemini = to_gemini_schema(REPLY_SCHEMA.schema)
    assert gemini["type"] == "OBJECT" and "additionalProperties" not in gemini
    assert gemini["properties"]["needs_human"]["type"] == "BOOLEAN"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ProviderAuthError),
        (402, ProviderBillingError),
        (429, ProviderRateLimited),
        (503, ProviderUnavailable),
        (400, ProviderBadRequest),
    ],
)
def test_http_error_taxonomy(status: int, error: type[Exception]) -> None:
    import httpx

    with pytest.raises(error):
        raise_for_status(httpx.Response(status, json={"error": {"message": "m"}}))
    assert ProviderRateLimited.retryable and not ProviderAuthError.retryable
