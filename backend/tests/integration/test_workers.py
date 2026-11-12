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
