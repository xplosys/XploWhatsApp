"""Hourly privacy sweep. Each step is independent and safe to repeat; one failing
business doesn't stop the others."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import app_transaction
from app.modules.privacy import service
from app.modules.privacy.models import DataExport, ExportStatus
from app.modules.storage import s3
from app.workers.dispatch import DATA_EXPORT, dispatch

log = structlog.get_logger(__name__)


async def _exports() -> dict[str, int]:
    expired = requeued = failed = 0
    async with app_transaction() as session:
        due = (await session.execute(text("SELECT * FROM app_data_exports_due(500)"))).all()
    for tenant_id, export_id in due:
        try:
            async with app_transaction(tenant_id) as session:
                export = await session.get(DataExport, export_id, with_for_update=True)
                if export is None:
                    continue
                if export.status == ExportStatus.READY:
                    if export.object_key:
                        await s3.delete(export.object_key)
                    export.object_key = None
                    export.status = ExportStatus.EXPIRED
                    expired += 1
                elif export.status == ExportStatus.QUEUED:
                    await dispatch(DATA_EXPORT, export.id, tenant_id)
                    export.updated_at = service.utcnow()
                    requeued += 1
                elif export.status == ExportStatus.RUNNING:
                    export.status = ExportStatus.FAILED
                    export.error = "The export was interrupted. Please request a new one."
                    failed += 1
        except Exception:
            log.exception("export_sweep_failed", export_id=str(export_id))
    return {"exports_expired": expired, "exports_requeued": requeued, "exports_failed": failed}


async def _purges() -> dict[str, int]:
    purged = 0
    async with app_transaction() as session:
        due = (await session.execute(text("SELECT tenant_id FROM app_tenants_due_purge(20)"))).scalars().all()
    for tenant_id in due:
        try:
            removed = await service.purge_business(tenant_id)
            log.info("business_purged", tenant_id=str(tenant_id), removed=removed)
            purged += 1
        except Exception:
            log.exception("business_purge_failed", tenant_id=str(tenant_id))
    return {"businesses_purged": purged}


async def _retention() -> dict[str, int]:
    removed = 0
    async with app_transaction() as session:
        rows = (await session.execute(text("SELECT * FROM app_tenants_with_retention(10000)"))).all()
    for tenant_id, days in rows:
        try:
            removed += await service.apply_message_retention(tenant_id, days)
        except Exception:
            log.exception("retention_failed", tenant_id=str(tenant_id))
    return {"messages_removed": removed}


async def _webhook_payloads() -> dict[str, int]:
    days = get_settings().webhook_event_retention_days
    async with app_transaction() as session:
        n = await session.scalar(text("SELECT app_purge_webhook_events(:d)"), {"d": days})
    return {"webhook_events_removed": int(n or 0)}


async def sweep() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for step in (_exports, _purges, _retention, _webhook_payloads):
        try:
            out.update(await step())
        except Exception:
            log.exception("privacy_sweep_step_failed", step=step.__name__)
    return out
