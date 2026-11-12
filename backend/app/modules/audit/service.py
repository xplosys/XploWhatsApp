from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog


class RequestMeta:
    """Who and where a request came from, captured once per request for auditing."""

    __slots__ = ("ip", "request_id", "user_agent")

    def __init__(self, ip: str | None = None, user_agent: str | None = None, request_id: str | None = None):
        self.ip = ip
        self.user_agent = (user_agent or "")[:512] or None
        self.request_id = request_id


async def record(
    session: AsyncSession,
    action: str,
    *,
    tenant_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    meta: RequestMeta | None = None,
    target_type: str | None = None,
    target_id: str | uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id else None,
            details=details or {},
            ip=meta.ip if meta and meta.ip and meta.ip != "unknown" else None,
            user_agent=meta.user_agent if meta else None,
            request_id=meta.request_id if meta else None,
        )
    )
