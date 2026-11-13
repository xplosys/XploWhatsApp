"""Re-dispatch knowledge work that was stored but never finished: a version still
queued after a lost dispatch, a version left `processing` by a crashed worker, or a
re-index that stopped. Cross-tenant listing goes through SECURITY DEFINER functions
that return only IDs."""

from __future__ import annotations

from sqlalchemy import text

from app.db.session import app_transaction
from app.workers.dispatch import KNOWLEDGE_PROCESS, KNOWLEDGE_REINDEX, dispatch


async def sweep(queued_after_seconds: int = 120, processing_after_seconds: int = 900) -> dict[str, int]:
    async with app_transaction() as session:
        versions = (
            await session.execute(
                text("SELECT id, tenant_id FROM app_stale_knowledge_versions(:q, :p, 200)"),
                {"q": queued_after_seconds, "p": processing_after_seconds},
            )
        ).all()
        reindexes = (
            await session.execute(text("SELECT tenant_id FROM app_pending_knowledge_reindex(200)"))
        ).all()
    for row in versions:
        await dispatch(KNOWLEDGE_PROCESS, row.id, row.tenant_id)
    for row in reindexes:
        await dispatch(KNOWLEDGE_REINDEX, row.tenant_id)
    return {"versions": len(versions), "reindexes": len(reindexes)}
