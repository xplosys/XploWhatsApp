"""Provider registry and API-key resolution.

A key is resolved per call: the business's own key (decrypted only for that call), or
XploSys's platform key when the business chose it and its plan allows it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import crypto
from app.modules.ai.models import AIProviderConfig, KeySource, ProviderKey
from app.modules.ai.providers.base import AIProvider
from app.modules.ai.providers.deepseek import DeepSeekProvider
from app.modules.ai.providers.gemini import GeminiProvider
from app.modules.ai.providers.openai import OpenAIProvider
from app.modules.billing.service import effective_plan

ADAPTERS: dict[str, type[OpenAIProvider] | type[GeminiProvider] | type[DeepSeekProvider]] = {
    ProviderKey.OPENAI: OpenAIProvider,
    ProviderKey.GEMINI: GeminiProvider,
    ProviderKey.DEEPSEEK: DeepSeekProvider,
}

PROVIDER_LABELS = {
    ProviderKey.GEMINI: "Google Gemini",
    ProviderKey.OPENAI: "OpenAI",
    ProviderKey.DEEPSEEK: "DeepSeek",
}


def build(provider: str, api_key: str) -> AIProvider:
    return ADAPTERS[provider](api_key)


def platform_key(provider: str) -> str | None:
    settings = get_settings()
    secret = {
        ProviderKey.OPENAI: settings.openai_api_key,
        ProviderKey.GEMINI: settings.gemini_api_key,
        ProviderKey.DEEPSEEK: settings.deepseek_api_key,
    }.get(ProviderKey(provider))
    return secret.get_secret_value() if secret else None


def _aad(tenant_id: uuid.UUID, provider: str) -> str:
    return f"ai_key:{tenant_id}:{provider}"


def set_tenant_key(config: AIProviderConfig, api_key: str) -> None:
    ciphertext = crypto.encrypt(api_key, _aad(config.tenant_id, config.provider))
    config.api_key_ciphertext = ciphertext
    config.key_version = crypto.key_version_of(ciphertext)
    config.key_hint = api_key[-4:] if len(api_key) >= 12 else None


def clear_tenant_key(config: AIProviderConfig) -> None:
    config.api_key_ciphertext = None
    config.key_version = None
    config.key_hint = None


async def platform_keys_allowed(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    return (await effective_plan(session, tenant_id)).limit("platform_ai_keys", 0) > 0


async def provider_configs(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, AIProviderConfig]:
    rows = (
        await session.execute(select(AIProviderConfig).where(AIProviderConfig.tenant_id == tenant_id))
    ).scalars()
    return {c.provider: c for c in rows}


@dataclass(frozen=True)
class ResolvedKey:
    provider: str
    api_key: str
    key_source: str


async def resolve_key(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    config: AIProviderConfig | None,
    *,
    require_enabled: bool = True,
) -> ResolvedKey | None:
    """The key to use for this provider, or None if the business cannot use it now."""
    if config is None or (require_enabled and not config.enabled):
        return None
    if config.key_source == KeySource.PLATFORM:
        key = platform_key(config.provider)
        if key and await platform_keys_allowed(session, tenant_id):
            return ResolvedKey(config.provider, key, KeySource.PLATFORM)
        return None
    if config.api_key_ciphertext:
        key = crypto.decrypt(config.api_key_ciphertext, _aad(tenant_id, config.provider))
        return ResolvedKey(config.provider, key, KeySource.TENANT)
    return None
