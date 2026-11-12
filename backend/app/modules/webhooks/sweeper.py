"""Recovery sweeps run by Celery beat.

The app role cannot list other tenants' rows, so cross-tenant queues are read
through SECURITY DEFINER functions that return only (id, tenant_id). Each item is
then processed inside its own tenant-bound transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text

from app.db.session import app_transaction
from app.workers.dispatch import PROCESS_WEBHOOK_EVENT, SEND_OUTBOX_JOB, dispatch


@dataclass(frozen=True)
class WorkItem:
    id: uuid.UUID
    tenant_id: uuid.UUID


async def _fetch(sql: str, params: dict[str, object]) -> list[WorkItem]:
    async with app_transaction() as session:
        rows = (await session.execute(text(sql), params)).all()
    return [WorkItem(r.id, r.tenant_id) for r in rows]


async def due_webhook_events(limit: int = 200, min_age_seconds: int = 30) -> list[WorkItem]:
    return await _fetch(
        "SELECT id, tenant_id FROM app_due_webhook_events(:l, :a)", {"l": limit, "a": min_age_seconds}
    )


async def due_outbox_jobs(limit: int = 200, min_age_seconds: int = 15) -> list[WorkItem]:
    return await _fetch(
        "SELECT id, tenant_id FROM app_due_outbox_jobs(:l, :a)", {"l": limit, "a": min_age_seconds}
    )


async def stale_sending_jobs(older_than_seconds: int = 600, limit: int = 200) -> list[WorkItem]:
    return await _fetch(
        "SELECT id, tenant_id FROM app_stale_outbox_jobs(:s, :l)", {"s": older_than_seconds, "l": limit}
    )


async def accounts_for_health_check(limit: int = 500) -> list[WorkItem]:
    return await _fetch("SELECT id, tenant_id FROM app_whatsapp_accounts_to_check(:l)", {"l": limit})


async def sweep() -> dict[str, int]:
    """Re-dispatch work that was stored but never picked up (broker outage, crash,
    or a scheduled retry that is now due)."""
    from app.modules.conversations.outbound import mark_stale_sending

    events = await due_webhook_events()
    for item in events:
        await dispatch(PROCESS_WEBHOOK_EVENT, item.id, item.tenant_id)
    jobs = await due_outbox_jobs()
    for item in jobs:
        await dispatch(SEND_OUTBOX_JOB, item.id, item.tenant_id)
    stale = await stale_sending_jobs()
    for item in stale:
        await mark_stale_sending(item.id, item.tenant_id)
    return {"webhook_events": len(events), "outbox_jobs": len(jobs), "stale_jobs": len(stale)}
