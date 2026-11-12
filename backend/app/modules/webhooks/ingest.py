"""Persist-then-acknowledge webhook ingestion.

A Meta POST can carry several entries (WABAs) and changes. Each change becomes
one webhook_events row, routed to its tenant through narrow SECURITY DEFINER
lookups (the app role cannot read other tenants' numbers). Exact redeliveries
are dropped by the unique dedup key; message-level duplicates are dropped again
by the unique WhatsApp message ID when processed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import uuid7
from app.db.session import set_tenant_context
from app.modules.webhooks.models import WebhookEvent

WHATSAPP_OBJECT = "whatsapp_business_account"


@dataclass(frozen=True)
class Route:
    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    number_id: uuid.UUID | None


@dataclass(frozen=True)
class StoredEvent:
    id: uuid.UUID
    tenant_id: uuid.UUID


def dedup_key(field: str, entry_id: str | None, value: dict[str, Any]) -> str:
    canonical = json.dumps({"f": field, "e": entry_id, "v": value}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def resolve_by_phone_number_id(session: AsyncSession, phone_number_id: str) -> Route | None:
    row = (
        await session.execute(
            text("SELECT tenant_id, account_id, number_id FROM app_resolve_whatsapp_number(:p)"),
            {"p": phone_number_id},
        )
    ).first()
    return Route(row.tenant_id, row.account_id, row.number_id) if row else None


async def resolve_by_waba_id(session: AsyncSession, waba_id: str) -> Route | None:
    row = (
        await session.execute(
            text("SELECT tenant_id, account_id FROM app_resolve_whatsapp_account(:w)"), {"w": waba_id}
        )
    ).first()
    return Route(row.tenant_id, row.account_id, None) if row else None


async def ingest(session: AsyncSession, payload: dict[str, Any]) -> list[StoredEvent]:
    """Store every change in the payload. Returns the routed, newly stored events
    to hand to workers. Must run inside one transaction."""
    stored: list[StoredEvent] = []
    if payload.get("object") != WHATSAPP_OBJECT:
        return stored
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry["id"]) if entry.get("id") is not None else None
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            field = str(change.get("field") or "unknown")[:64]
            raw_value = change.get("value")
            value: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
            phone_number_id = str((value.get("metadata") or {}).get("phone_number_id") or "") or None

            route = await resolve_by_phone_number_id(session, phone_number_id) if phone_number_id else None
            if route is None and entry_id:
                route = await resolve_by_waba_id(session, entry_id)

            event_id = uuid7()
            row = {
                "id": event_id,
                "tenant_id": route.tenant_id if route else None,
                "field": field,
                "waba_id": entry_id,
                "phone_number_id": phone_number_id,
                "dedup_key": dedup_key(field, entry_id, value),
                "payload": {"entry_id": entry_id, "field": field, "value": value},
                "status": "pending" if route else "unroutable",
            }
            await set_tenant_context(session, route.tenant_id if route else None)
            if route is None:
                # The row is invisible to this role, so neither RETURNING nor ON CONFLICT
                # (which applies the SELECT policy) can be used: rely on the unique key.
                try:
                    async with session.begin_nested():
                        await session.execute(insert(WebhookEvent).values(**row))
                except IntegrityError:
                    pass  # duplicate delivery
                continue
            stmt = insert(WebhookEvent).values(**row).on_conflict_do_nothing(index_elements=["dedup_key"])
            inserted = (await session.execute(stmt.returning(WebhookEvent.id))).scalar_one_or_none()
            if inserted is not None:
                stored.append(StoredEvent(inserted, route.tenant_id))
    await set_tenant_context(session, None)
    return stored
