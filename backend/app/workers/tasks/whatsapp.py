"""Scheduled WhatsApp maintenance (queue: default): recovery sweeps, account health
checks and template sync."""

from __future__ import annotations

import uuid

import structlog

from app.workers.celery_app import celery_app
from app.workers.runtime import run_async

log = structlog.get_logger(__name__)


@celery_app.task(name="app.workers.tasks.whatsapp.sweep_queues")
def sweep_queues() -> dict[str, int]:
    from app.modules.webhooks.sweeper import sweep

    return run_async(sweep())


async def check_and_sync(account_id: uuid.UUID, tenant_id: uuid.UUID, sync_templates: bool) -> str:
    """Health check in one transaction, then template sync in another, so a sync
    failure never rolls back the health result."""
    from app.core.errors import AppError
    from app.db.session import app_transaction
    from app.modules.whatsapp import service
    from app.modules.whatsapp.graph import GraphClient
    from app.modules.whatsapp.models import AccountStatus

    async with GraphClient() as graph:
        async with app_transaction(tenant_id) as session:
            account = await service.get_account(session, tenant_id, account_id)
            await service.check_health(session, account, graph)
            health, connected = account.health, account.status == AccountStatus.CONNECTED
        if sync_templates and connected:
            try:
                async with app_transaction(tenant_id) as session:
                    account = await service.get_account(session, tenant_id, account_id)
                    await service.sync_templates(session, account, graph)
            except AppError:
                log.info("template_sync_failed", account_id=str(account_id))
    return health


@celery_app.task(name="app.workers.tasks.whatsapp.check_account")
def check_account(account_id: str, tenant_id: str, sync_templates: bool = True) -> str:
    return run_async(check_and_sync(uuid.UUID(account_id), uuid.UUID(tenant_id), sync_templates))


@celery_app.task(name="app.workers.tasks.whatsapp.check_all_accounts")
def check_all_accounts() -> int:
    from app.modules.webhooks.sweeper import accounts_for_health_check

    items = run_async(accounts_for_health_check())
    for item in items:
        check_account.delay(str(item.id), str(item.tenant_id))
    return len(items)
