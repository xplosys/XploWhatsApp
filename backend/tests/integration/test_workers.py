from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from app.db.session import app_transaction
from app.modules.auth.models import RefreshToken
from app.workers.tasks.maintenance import purge_expired_auth_tokens_async
from tests.conftest import CSRF, RegisterFn


async def test_purge_expired_tokens_keeps_live_and_recent(register: RegisterFn) -> None:
    actor = await register("w@acme.in")
    await actor.client.post("/api/v1/auth/refresh", headers=CSRF)  # one rotated + one live token
    async with app_transaction() as s:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.revoked_at.is_not(None))
            .values(revoked_at=datetime.now(UTC) - timedelta(days=30))
        )
    result = await purge_expired_auth_tokens_async()
    assert result["refresh_tokens"] == 1
    async with app_transaction() as s:
        assert await s.scalar(select(func.count()).select_from(RefreshToken)) == 1


def test_celery_ping_task_eager() -> None:
    from app.workers.celery_app import celery_app
    from app.workers.tasks.system import ping

    celery_app.conf.task_always_eager = True
    try:
        assert ping.delay().get(timeout=5) == "pong"
    finally:
        celery_app.conf.task_always_eager = False


def test_whatsapp_tasks_are_registered_and_scheduled() -> None:
    from app.workers.celery_app import celery_app
    from app.workers.dispatch import PROCESS_WEBHOOK_EVENT, SEND_OUTBOX_JOB

    celery_app.loader.import_default_modules()
    for name in (
        PROCESS_WEBHOOK_EVENT,
        SEND_OUTBOX_JOB,
        "app.workers.tasks.whatsapp.sweep_queues",
        "app.workers.tasks.whatsapp.check_all_accounts",
        "app.workers.tasks.whatsapp.check_account",
    ):
        assert name in celery_app.tasks, name
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert {
        "app.workers.tasks.whatsapp.sweep_queues",
        "app.workers.tasks.whatsapp.check_all_accounts",
    } <= scheduled
    routes = celery_app.conf.task_routes
    assert routes["app.workers.tasks.messages.*"] == {"queue": "messages"}


def test_worker_process_sees_every_model() -> None:
    """A fresh worker process must resolve every mapper (foreign keys to tenants etc.),
    not only the models its task modules happen to import."""
    import subprocess
    import sys

    code = (
        "from app.workers.celery_app import celery_app\n"
        "celery_app.loader.import_default_modules()\n"
        "from app.db.base import Base\n"
        "import app.modules.conversations.outbound, app.modules.webhooks.processor\n"
        "for table in Base.metadata.tables.values():\n"
        "    for fk in table.foreign_keys:\n"
        "        fk.column\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr
