from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.api.deps import Requires, TenantContext
from app.api.pagination import Page, PageParams, page_params
from app.modules.audit.models import AuditLog
from app.modules.rbac.permissions import Permission

router = APIRouter(prefix="/audit-logs", tags=["audit"])


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    ip: Any | None
    occurred_at: datetime


@router.get("", response_model=Page[AuditLogOut])
async def list_audit_logs(
    ctx: Annotated[TenantContext, Requires(Permission.AUDIT_READ)],
    page: Annotated[PageParams, Depends(page_params)],
    action: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[AuditLogOut]:
    base = select(AuditLog).where(AuditLog.tenant_id == ctx.tenant_id)
    if action:
        base = base.where(AuditLog.action == action)
    total = await ctx.session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = (
        (
            await ctx.session.execute(
                base.order_by(AuditLog.occurred_at.desc()).limit(page.limit).offset(page.offset)
            )
        )
        .scalars()
        .all()
    )
    items = [
        AuditLogOut.model_validate(r).model_copy(update={"ip": str(r.ip) if r.ip else None}) for r in rows
    ]
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)
