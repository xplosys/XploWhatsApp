from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import CurrentUser, Meta, Requires, TenantContext
from app.api.pagination import Page, PageParams, page_params
from app.modules.auth.schemas import MembershipSummary
from app.modules.auth.sessions import list_memberships
from app.modules.rbac.permissions import Permission
from app.modules.tenants import service
from app.modules.tenants.schemas import (
    InvitationCreate,
    InvitationOut,
    MemberOut,
    MemberUpdate,
    TenantOut,
    TenantUpdate,
)

router = APIRouter(prefix="/tenants", tags=["tenants"])
Pagination = Annotated[PageParams, Depends(page_params)]


@router.get("", response_model=list[MembershipSummary])
async def my_tenants(ctx: CurrentUser) -> list[MembershipSummary]:
    """Businesses the signed-in user belongs to."""
    return await list_memberships(ctx.session, ctx.user.id)


@router.get("/current", response_model=TenantOut)
async def get_current(ctx: Annotated[TenantContext, Requires(Permission.TENANT_READ)]) -> TenantOut:
    return TenantOut.model_validate(ctx.tenant)


@router.put("/current", response_model=TenantOut)
async def update_current(
    body: TenantUpdate, ctx: Annotated[TenantContext, Requires(Permission.TENANT_UPDATE)], meta: Meta
) -> TenantOut:
    return TenantOut.model_validate(await service.update_tenant(ctx, body, meta))


@router.get("/current/members", response_model=Page[MemberOut])
async def list_members(
    ctx: Annotated[TenantContext, Requires(Permission.MEMBERS_READ)], page: Pagination
) -> Page[MemberOut]:
    return await service.list_members(ctx, page)


@router.patch("/current/members/{membership_id}", response_model=MemberOut)
async def update_member(
    membership_id: uuid.UUID,
    body: MemberUpdate,
    ctx: Annotated[TenantContext, Requires(Permission.MEMBERS_MANAGE)],
    meta: Meta,
) -> MemberOut:
    return await service.update_member(ctx, membership_id, body, meta)


@router.delete("/current/members/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    membership_id: uuid.UUID, ctx: Annotated[TenantContext, Requires(Permission.MEMBERS_MANAGE)], meta: Meta
) -> Response:
    await service.remove_member(ctx, membership_id, meta)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/current/invitations", response_model=Page[InvitationOut])
async def list_invitations(
    ctx: Annotated[TenantContext, Requires(Permission.MEMBERS_INVITE)], page: Pagination
) -> Page[InvitationOut]:
    result = await service.list_invitations(ctx, page)
    return Page(
        items=[InvitationOut.model_validate(i) for i in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.post("/current/invitations", response_model=InvitationOut, status_code=status.HTTP_201_CREATED)
async def create_invitation(
    body: InvitationCreate, ctx: Annotated[TenantContext, Requires(Permission.MEMBERS_INVITE)], meta: Meta
) -> InvitationOut:
    return InvitationOut.model_validate(await service.create_invitation(ctx, body, meta))


@router.delete("/current/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    invitation_id: uuid.UUID, ctx: Annotated[TenantContext, Requires(Permission.MEMBERS_INVITE)], meta: Meta
) -> Response:
    await service.revoke_invitation(ctx, invitation_id, meta)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
