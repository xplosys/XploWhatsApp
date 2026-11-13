from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.modules.billing.gateways.base import from_minor, minor_units, with_tax
from app.modules.billing.gateways.stripe import form, parse_subscription
from app.modules.billing.gst import financial_year, split_tax, valid_gstin


def test_gstin_checksum_and_state() -> None:
    assert valid_gstin("27AAACB5678C1ZR")
    assert valid_gstin("29AAACX1234A1ZT")
    assert not valid_gstin("27AAACB5678C1ZX")  # wrong check digit
    assert not valid_gstin("99AAACB5678C1ZR")  # no such state
    assert not valid_gstin("27aaacb5678c1zr")
    assert not valid_gstin("27AAACB5678C1Z")


def test_financial_year_turns_over_on_1_april_in_india() -> None:
    # 31 March 19:00 UTC is already 1 April 00:30 in India.
    assert financial_year(datetime(2027, 3, 31, 18, 0, tzinfo=UTC)) == "2026-27"
    assert financial_year(datetime(2027, 3, 31, 19, 0, tzinfo=UTC)) == "2027-28"
    assert financial_year(datetime(2099, 12, 1, tzinfo=UTC)) == "2099-00"


def test_gst_split_within_and_between_states() -> None:
    sub, taxes = split_tax(Decimal("1180.00"), Decimal("18"), "29", "29")
    assert sub == Decimal("1000.00")
    assert taxes == [
        {"name": "CGST", "rate": "9", "amount": "90.00"},
        {"name": "SGST", "rate": "9", "amount": "90.00"},
    ]
    sub, taxes = split_tax(Decimal("1180.00"), Decimal("18"), "29", "27")
    assert taxes == [{"name": "IGST", "rate": "18", "amount": "180.00"}]
    # Odd paise: the halves still add up to the total.
    sub, taxes = split_tax(Decimal("100.01"), Decimal("18"), "29", "29")
    assert sub + sum(Decimal(t["amount"]) for t in taxes) == Decimal("100.01")
    assert split_tax(Decimal("15"), Decimal(0), "29", None) == (Decimal("15.00"), [])


def test_money_helpers() -> None:
    assert with_tax(Decimal("4999"), Decimal("18")) == Decimal("5898.82")
    assert minor_units(Decimal("5898.82")) == 589882
    assert from_minor(589882) == Decimal("5898.82")


def test_stripe_form_encoding() -> None:
    assert form({"items": [{"id": "si_1", "deleted": True}, {"price": "p", "quantity": 2}], "x": None}) == [
        ("items[0][id]", "si_1"),
        ("items[0][deleted]", "true"),
        ("items[1][price]", "p"),
        ("items[1][quantity]", "2"),
    ]
    assert form({"metadata": {"tenant_id": "t"}, "expand": ["a", "b"]}) == [
        ("metadata[tenant_id]", "t"),
        ("expand[0]", "a"),
        ("expand[1]", "b"),
    ]


def test_stripe_period_from_items_on_newer_api_versions() -> None:
    gsub = parse_subscription(
        {
            "id": "sub_1",
            "status": "trialing",
            "customer": "cus_1",
            "items": {
                "data": [{"price": {"id": "price_1"}, "current_period_start": 100, "current_period_end": 200}]
            },
        }
    )
    assert gsub.status == "active" and gsub.gateway_plan_id == "price_1"
    assert gsub.period_end == datetime.fromtimestamp(200, UTC)
