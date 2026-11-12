"""The assistant's settings: a validated, versioned document per business."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.modules.ai.models import Assistant, AssistantVersion, ProviderKey

WHATSAPP_TEXT_LIMIT = 4096


class ModelChoice(BaseModel):
    provider: ProviderKey
    model: str = Field(min_length=1, max_length=200)


class AssistantConfig(BaseModel):
    """What the business tells its assistant. Facts live here (and, from Phase 4, in the
    knowledge base); the validator rejects prices, times or contacts that appear in a
    reply but nowhere in these facts or the conversation."""

    assistant_name: str = Field(default="Assistant", min_length=1, max_length=60)
    business_description: str = Field(default="", max_length=4000)
    instructions: str = Field(
        default="",
        max_length=20000,
        description="Facts, FAQs, services, prices and policies the assistant may use.",
    )
    business_hours: str = Field(default="", max_length=1000)
    tone: Literal["friendly", "professional", "concise"] = "friendly"
    reply_language: Literal["match_customer", "business_default"] = "match_customer"
    handover_message: str = Field(
        default="Thanks for your message. A member of our team will reply to you shortly.",
        min_length=1,
        max_length=1000,
    )
    max_reply_chars: int = Field(default=1000, ge=100, le=WHATSAPP_TEXT_LIMIT)
    primary: ModelChoice
    fallbacks: list[ModelChoice] = Field(default_factory=list, max_length=3)

    @field_validator("business_description", "instructions", "business_hours", "handover_message")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _distinct_chain(self) -> AssistantConfig:
        seen = set()
        for choice in [self.primary, *self.fallbacks]:
            key = (choice.provider, choice.model)
            if key in seen:
                raise ValueError("Each model may appear only once in the fallback chain.")
            seen.add(key)
        return self

    @property
    def chain(self) -> list[ModelChoice]:
        return [self.primary, *self.fallbacks]


async def get_assistant(
    session: AsyncSession, tenant_id: uuid.UUID, *, lock: bool = False
) -> Assistant | None:
    stmt = select(Assistant).where(Assistant.tenant_id == tenant_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_version(session: AsyncSession, assistant: Assistant, version: int) -> AssistantVersion:
    row = (
        await session.execute(
            select(AssistantVersion).where(
                AssistantVersion.bot_configuration_id == assistant.id, AssistantVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("That version does not exist.")
    return row


async def active_config(session: AsyncSession, assistant: Assistant) -> AssistantConfig | None:
    if assistant.active_version is None:
        return None
    version = await get_version(session, assistant, assistant.active_version)
    return AssistantConfig.model_validate(version.config)


async def save_version(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    config: AssistantConfig,
    *,
    name: str | None,
    user_id: uuid.UUID | None,
) -> tuple[Assistant, AssistantVersion]:
    assistant = await get_assistant(session, tenant_id, lock=True)
    if assistant is None:
        assistant = Assistant(
            tenant_id=tenant_id, name=name or "WhatsApp assistant", auto_reply_enabled=False
        )
        session.add(assistant)
        await session.flush()
    elif name:
        assistant.name = name
    latest = await session.scalar(
        select(func.max(AssistantVersion.version)).where(
            AssistantVersion.bot_configuration_id == assistant.id
        )
    )
    version = AssistantVersion(
        tenant_id=tenant_id,
        bot_configuration_id=assistant.id,
        version=int(latest or 0) + 1,
        config=config.model_dump(mode="json"),
        created_by_user_id=user_id,
        created_at=datetime.now(UTC),
    )
    session.add(version)
    await session.flush()
    assistant.active_version = version.version
    return assistant, version


async def list_versions(session: AsyncSession, assistant: Assistant) -> list[AssistantVersion]:
    return list(
        (
            await session.execute(
                select(AssistantVersion)
                .where(AssistantVersion.bot_configuration_id == assistant.id)
                .order_by(AssistantVersion.version.desc())
            )
        ).scalars()
    )
