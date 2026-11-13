"""GST helpers: state codes, GSTIN validation, financial years and the tax split."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.modules.billing.gateways.base import money

# GST state and union territory codes (first two digits of a GSTIN).
STATES: dict[str, str] = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
}

_GSTIN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
IST = ZoneInfo("Asia/Kolkata")


def gstin_check_digit(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        product = _CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return _CHARS[(36 - total % 36) % 36]


def valid_gstin(gstin: str) -> bool:
    return bool(_GSTIN.match(gstin)) and gstin[:2] in STATES and gstin_check_digit(gstin[:14]) == gstin[14]


def financial_year(when: datetime) -> str:
    """Indian financial year (April to March) of a moment, e.g. "2026-27"."""
    local = when.astimezone(IST)
    start = local.year if local.month >= 4 else local.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def split_tax(
    total: Decimal, rate: Decimal, seller_state: str | None, buyer_state: str | None
) -> tuple[Decimal, list[dict[str, str]]]:
    """Split a tax-inclusive total into (subtotal, tax lines). Within one state the tax
    is CGST and SGST in halves; between states it is IGST."""
    if rate <= 0:
        return money(total), []
    subtotal = money(total / (1 + rate / 100))
    tax = money(total - subtotal)
    if seller_state and buyer_state == seller_state:
        half_rate = rate / 2
        cgst = money(tax / 2)
        return subtotal, [
            {"name": "CGST", "rate": str(half_rate.normalize()), "amount": str(cgst)},
            {"name": "SGST", "rate": str(half_rate.normalize()), "amount": str(money(tax - cgst))},
        ]
    return subtotal, [{"name": "IGST", "rate": str(rate.normalize()), "amount": str(tax)}]
