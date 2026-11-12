from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_

from app.db.session import app_transaction
from app.modules.auth.models import RefreshToken, UserActionToken
from app.workers.celery_app import celery_app
from app.workers.runtime import run_async

# Keep revoked tokens briefly so refresh-token reuse can still be detected.
REVOKED_RETENTION = timedelta(days=7)


async def purge_expired_auth_tokens_async() -> dict[str, int]:
    now = datetime.now(UTC)
    async with app_transaction() as session:
        refresh = await session.execute(
            delete(RefreshToken).where(
                or_(RefreshToken.expires_at < now, RefreshToken.revoked_at < now - REVOKED_RETENTION)
            )
        )
        action = await session.execute(delete(UserActionToken).where(UserActionToken.expires_at < now))
    return {"refresh_tokens": refresh.rowcount or 0, "action_tokens": action.rowcount or 0}  # type: ignore[attr-defined]


@celery_app.task(name="app.workers.tasks.maintenance.purge_expired_auth_tokens")
def purge_expired_auth_tokens() -> dict[str, int]:
    return run_async(purge_expired_auth_tokens_async())
