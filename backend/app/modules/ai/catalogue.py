"""Model catalogue, dated prices, cost estimates and usage metering."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.models import AIModel, AIModelPrice, UsageRecord
from app.modules.ai.providers.base import ModelInfo, Usage

MILLION = Decimal(1_000_000)


async def find_model(session: AsyncSession, provider: str, model_id: str) -> AIModel | None:
    return (
        await session.execute(
            select(AIModel).where(AIModel.provider == provider, AIModel.model_id == model_id)
        )
    ).scalar_one_or_none()


async def active_models(session: AsyncSession, provider: str | None = None) -> list[AIModel]:
    stmt = select(AIModel).where(AIModel.active.is_(True)).order_by(AIModel.provider, AIModel.model_id)
    if provider:
        stmt = stmt.where(AIModel.provider == provider)
    return list((await session.execute(stmt)).scalars())


async def upsert_models(session: AsyncSession, provider: str, models: list[ModelInfo]) -> tuple[int, int]:
    """Add newly listed models (inactive until a Super Admin turns them on) and refresh
    metadata for known ones. Returns (added, updated)."""
    now = datetime.now(UTC)
    existing = {
        m.model_id: m
        for m in (await session.execute(select(AIModel).where(AIModel.provider == provider))).scalars()
    }
    added = updated = 0
    for info in models:
        model = existing.get(info.model_id)
        if model is None:
            session.add(
                AIModel(
                    provider=provider,
                    model_id=info.model_id,
                    display_name=info.display_name,
                    context_window=info.context_window,
                    max_output_tokens=info.max_output_tokens,
                    capabilities=info.capabilities,
                    active=False,
                    last_seen_at=now,
                )
            )
            added += 1
        else:
            model.last_seen_at = now
            model.display_name = model.display_name or info.display_name
            model.context_window = info.context_window or model.context_window
            model.max_output_tokens = info.max_output_tokens or model.max_output_tokens
            updated += 1
    await session.flush()
    return added, updated


async def price_at(session: AsyncSession, provider: str, model_id: str, at: datetime) -> AIModelPrice | None:
    return (
        await session.execute(
            select(AIModelPrice)
            .join(AIModel, AIModel.id == AIModelPrice.model_pk)
            .where(
                AIModel.provider == provider, AIModel.model_id == model_id, AIModelPrice.effective_from <= at
            )
            .order_by(AIModelPrice.effective_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def estimate(price: AIModelPrice, usage: Usage) -> Decimal:
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    cached_rate = (
        price.cached_input_per_mtok if price.cached_input_per_mtok is not None else price.input_per_mtok
    )
    cost = (
        Decimal(usage.input_tokens - cached) * price.input_per_mtok
        + Decimal(cached) * cached_rate
        + Decimal(usage.output_tokens) * price.output_per_mtok
    ) / MILLION
    return cost.quantize(Decimal("0.00000001"))


@dataclass(frozen=True)
class CallRecord:
    provider: str
    model: str
    key_source: str
    usage: Usage
    outcome: str  # ok, or an error code
    purpose: str  # auto_reply, preview, test
    latency_ms: int
    conversation_id: uuid.UUID | None = None


async def record_call(session: AsyncSession, tenant_id: uuid.UUID, call: CallRecord) -> UsageRecord:
    now = datetime.now(UTC)
    price = await price_at(session, call.provider, call.model, now)
    has_tokens = call.usage.input_tokens or call.usage.output_tokens
    dims: dict[str, Any] = {
        "provider": call.provider,
        "model": call.model,
        "key_source": call.key_source,
        "input_tokens": call.usage.input_tokens,
        "output_tokens": call.usage.output_tokens,
        "cached_input_tokens": call.usage.cached_input_tokens,
        "outcome": call.outcome,
        "purpose": call.purpose,
        "latency_ms": call.latency_ms,
    }
    if call.conversation_id:
        dims["conversation_id"] = str(call.conversation_id)
    record = UsageRecord(
        tenant_id=tenant_id,
        metric="ai_call",
        quantity=Decimal(1),
        unit="call",
        cost_estimate=estimate(price, call.usage) if price and has_tokens else None,
        currency=price.currency if price and has_tokens else None,
        cost_source=("catalogue" if price else "unpriced") if has_tokens else "none",
        dimensions=dims,
        occurred_at=now,
    )
    session.add(record)
    await session.flush()
    return record


async def record_reply(session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
    session.add(
        UsageRecord(
            tenant_id=tenant_id,
            metric="ai_reply",
            quantity=Decimal(1),
            unit="message",
            cost_source="none",
            dimensions={"conversation_id": str(conversation_id)},
            occurred_at=datetime.now(UTC),
        )
    )
    await session.flush()


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def replies_this_month(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(UsageRecord)
            .where(
                UsageRecord.tenant_id == tenant_id,
                UsageRecord.metric == "ai_reply",
                UsageRecord.occurred_at >= month_start(),
            )
        )
        or 0
    )
