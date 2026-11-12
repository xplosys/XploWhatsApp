"""Super Admin: the platform's AI model catalogue and its dated prices.

Models come from each provider's own model list (using XploSys's platform key) or are
added by hand. New models start inactive; prices are entered from the provider's
pricing page with the date they apply from. Nothing here is hard-coded.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Meta, SuperAdmin
from app.core.errors import AppError, Conflict, NotFound, ValidationFailed
from app.db.session import admin_transaction
from app.modules.ai import catalogue, registry
from app.modules.ai.models import AIModel, AIModelPrice, ProviderKey
from app.modules.ai.providers.base import ProviderError
from app.modules.ai.schemas import (
    AdminModelCreate,
    AdminModelOut,
    AdminModelUpdate,
    ModelSyncIn,
    ModelSyncOut,
    PriceIn,
    PriceOut,
)
from app.modules.audit import service as audit

router = APIRouter(prefix="/admin/ai", tags=["admin"])


class ProviderSyncFailed(AppError):
    status_code = 502
    code = "provider_sync_failed"


async def _out(session: AsyncSession, model: AIModel) -> AdminModelOut:
    out = AdminModelOut.model_validate(model)
    price = await catalogue.price_at(session, model.provider, model.model_id, datetime.now(UTC))
    out.current_price = PriceOut.model_validate(price) if price else None
    return out


@router.get("/models", response_model=list[AdminModelOut])
async def list_models(ctx: SuperAdmin, provider: ProviderKey | None = None) -> list[AdminModelOut]:
    async with admin_transaction() as session:
        stmt = select(AIModel).order_by(AIModel.provider, AIModel.active.desc(), AIModel.model_id)
        if provider:
            stmt = stmt.where(AIModel.provider == provider)
        return [await _out(session, m) for m in (await session.execute(stmt)).scalars()]


@router.post("/models", response_model=AdminModelOut, status_code=201)
async def add_model(body: AdminModelCreate, ctx: SuperAdmin, meta: Meta) -> AdminModelOut:
    async with admin_transaction() as session:
        if await catalogue.find_model(session, body.provider, body.model_id):
            raise Conflict("This model is already in the catalogue.", code="model_exists")
        model = AIModel(**body.model_dump(), capabilities={})
        session.add(model)
        await session.flush()
        await audit.record(
            session,
            "admin.ai_model_added",
            tenant_id=None,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="ai_model",
            target_id=model.id,
            details={"provider": body.provider.value, "model": body.model_id},
        )
        return await _out(session, model)


@router.patch("/models/{model_id}", response_model=AdminModelOut)
async def update_model(
    model_id: uuid.UUID, body: AdminModelUpdate, ctx: SuperAdmin, meta: Meta
) -> AdminModelOut:
    async with admin_transaction() as session:
        model = await session.get(AIModel, model_id, with_for_update=True)
        if model is None:
            raise NotFound("Model not found.")
        changes = body.model_dump(exclude_unset=True)
        for field_name, value in changes.items():
            setattr(model, field_name, value)
        await audit.record(
            session,
            "admin.ai_model_updated",
            tenant_id=None,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="ai_model",
            target_id=model.id,
            details=changes,
        )
        await session.flush()
        return await _out(session, model)


@router.post("/models/sync", response_model=ModelSyncOut)
async def sync_models(body: ModelSyncIn, ctx: SuperAdmin, meta: Meta) -> ModelSyncOut:
    key = registry.platform_key(body.provider)
    if not key:
        raise ValidationFailed(
            f"Set {body.provider.value.upper()}_API_KEY on the server to list this provider's models.",
            code="no_platform_key",
        )
    try:
        models = await registry.build(body.provider, key).list_models()
    except ProviderError as exc:
        raise ProviderSyncFailed(f"The provider refused the request: {exc}") from exc
    async with admin_transaction() as session:
        added, updated = await catalogue.upsert_models(session, body.provider, models)
        await audit.record(
            session,
            "admin.ai_models_synced",
            tenant_id=None,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="ai_provider",
            target_id=body.provider.value,
            details={"added": added, "updated": updated},
        )
    return ModelSyncOut(provider=body.provider.value, added=added, updated=updated)


@router.get("/models/{model_id}/prices", response_model=list[PriceOut])
async def list_prices(model_id: uuid.UUID, ctx: SuperAdmin) -> list[PriceOut]:
    async with admin_transaction() as session:
        rows = (
            await session.execute(
                select(AIModelPrice)
                .where(AIModelPrice.model_pk == model_id)
                .order_by(AIModelPrice.effective_from.desc())
            )
        ).scalars()
        return [PriceOut.model_validate(p) for p in rows]


@router.post("/models/{model_id}/prices", response_model=PriceOut, status_code=201)
async def add_price(model_id: uuid.UUID, body: PriceIn, ctx: SuperAdmin, meta: Meta) -> PriceOut:
    async with admin_transaction() as session:
        model = await session.get(AIModel, model_id)
        if model is None:
            raise NotFound("Model not found.")
        price = AIModelPrice(
            model_pk=model.id,
            input_per_mtok=body.input_per_mtok,
            output_per_mtok=body.output_per_mtok,
            cached_input_per_mtok=body.cached_input_per_mtok,
            currency=body.currency,
            effective_from=body.effective_from or datetime.now(UTC),
            source_note=body.source_note,
            created_by_user_id=ctx.user.id,
        )
        session.add(price)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError as exc:
            raise Conflict("A price already starts at that time.", code="price_exists") from exc
        await audit.record(
            session,
            "admin.ai_price_added",
            tenant_id=None,
            actor_user_id=ctx.user.id,
            meta=meta,
            target_type="ai_model",
            target_id=model.id,
            details={k: str(v) for k, v in body.model_dump().items() if v is not None},
        )
        return PriceOut.model_validate(price)
