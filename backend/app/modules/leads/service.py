"""Lead rules: who may own a lead, the history written for every change, and the
currency a business uses by default."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.modules.conversations.inbox import active_member
from app.modules.leads.models import CLOSED_STATUSES, Lead, LeadActivity, LeadStatus

# ISO 4217 code a business in this country most likely quotes prices in. Only a
# default for the form; any three-letter code can be stored.
COUNTRY_CURRENCY = {
    "IN": "INR",
    "GB": "GBP",
    "US": "USD",
    "AE": "AED",
    "SA": "SAR",
    "SG": "SGD",
    "AU": "AUD",
    "CA": "CAD",
    "NZ": "NZD",
    "ZA": "ZAR",
    "NG": "NGN",
    "KE": "KES",
    "BR": "BRL",
    "MX": "MXN",
    "ID": "IDR",
    "MY": "MYR",
    "PK": "PKR",
    "BD": "BDT",
    "LK": "LKR",
    "NP": "NPR",
    "DE": "EUR",
    "FR": "EUR",
    "ES": "EUR",
    "IT": "EUR",
    "NL": "EUR",
    "IE": "EUR",
}


def default_currency(country: str | None) -> str | None:
    return COUNTRY_CURRENCY.get((country or "").upper())


def clean_currency(code: str | None) -> str | None:
    if code is None or not code.strip():
        return None
    code = code.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValidationFailed(
            "Use a three-letter currency code such as INR or USD.", code="invalid_currency"
        )
    return code


async def check_owner(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    can_assign_others: bool,
    current: uuid.UUID | None,
    new: uuid.UUID | None,
) -> None:
    """The same rule as conversations: without the assign permission a person may take
    an unowned lead or let go of their own."""
    if new == current:
        return
    if not can_assign_others:
        taking = new == actor_id and current is None
        letting_go = new is None and current == actor_id
        if not (taking or letting_go):
            raise Forbidden(
                "You can take unowned leads or let go of your own. "
                "A supervisor or admin can reassign others.",
                code="lead_owner_not_allowed",
            )
    if new is not None and not await active_member(session, tenant_id, new):
        raise ValidationFailed("That person is not an active member of this business.", code="not_a_member")


def log_activity(
    session: AsyncSession,
    lead: Lead,
    kind: str,
    actor_id: uuid.UUID | None,
    data: dict[str, Any] | None = None,
) -> LeadActivity:
    activity = LeadActivity(
        tenant_id=lead.tenant_id, lead_id=lead.id, type=kind, data=data or {}, actor_user_id=actor_id
    )
    session.add(activity)
    return activity


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def set_status(session: AsyncSession, lead: Lead, status: str, actor_id: uuid.UUID) -> None:
    if status == lead.status:
        return
    log_activity(session, lead, "status_changed", actor_id, {"from": lead.status, "to": status})
    lead.status = status
    lead.closed_at = datetime.now(UTC) if status in CLOSED_STATUSES else None


def set_value(
    session: AsyncSession, lead: Lead, value: Decimal | None, currency: str | None, actor_id: uuid.UUID
) -> None:
    if value == lead.value and currency == lead.currency:
        return
    log_activity(
        session,
        lead,
        "value_changed",
        actor_id,
        {
            "from": _money(lead.value),
            "to": _money(value),
            "currency": currency,
            "previous_currency": lead.currency,
        },
    )
    lead.value, lead.currency = value, currency


async def get_lead(
    session: AsyncSession, tenant_id: uuid.UUID, lead_id: uuid.UUID, *, lock: bool = False
) -> Lead:
    lead = await session.get(Lead, lead_id, with_for_update=lock)
    if lead is None or lead.tenant_id != tenant_id:
        raise NotFound("Lead not found.", code="lead_not_found")
    return lead


__all__ = [
    "LeadStatus",
    "check_owner",
    "clean_currency",
    "default_currency",
    "get_lead",
    "log_activity",
    "set_status",
    "set_value",
]
