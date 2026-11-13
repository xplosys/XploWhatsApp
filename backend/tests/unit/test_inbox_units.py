from __future__ import annotations

import pytest

from app.core.errors import ValidationFailed
from app.modules.conversations.inbox import clean_tags
from app.modules.conversations.router import like_pattern
from app.modules.leads.service import clean_currency, default_currency


def test_tags_are_normalised_and_deduplicated() -> None:
    assert clean_tags(["  VIP ", "vip", "Follow   up", "", "kids & teens", "café"]) == [
        "vip",
        "follow up",
        "kids & teens",
        "café",
    ]


@pytest.mark.parametrize("bad", ["<b>", "-start", "x" * 41, "a,b", "semi;colon"])
def test_invalid_tags_are_refused(bad: str) -> None:
    with pytest.raises(ValidationFailed):
        clean_tags([bad])


def test_too_many_tags() -> None:
    with pytest.raises(ValidationFailed):
        clean_tags([f"t{i}" for i in range(21)])


def test_like_pattern_escapes_wildcards() -> None:
    assert like_pattern(" 50%_Off ") == "%50\\%\\_off%"
    assert like_pattern("a\\b") == "%a\\\\b%"


def test_currency_rules() -> None:
    assert (
        default_currency("in") == "INR" and default_currency("GB") == "GBP" and default_currency("XX") is None
    )
    assert clean_currency(" usd ") == "USD" and clean_currency("") is None and clean_currency(None) is None
    with pytest.raises(ValidationFailed):
        clean_currency("U5D")
