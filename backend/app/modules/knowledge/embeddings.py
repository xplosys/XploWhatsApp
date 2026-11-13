"""Turning text into vectors with the business's chosen embedding model."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.ai import registry
from app.modules.ai.catalogue import CallRecord
from app.modules.ai.models import AIModel, ModelKind, ProviderKey
from app.modules.ai.providers.base import EmbedTask, ProviderBadRequest, ProviderError, Usage
from app.modules.knowledge.models import EMBEDDING_DIMENSIONS


@dataclass(frozen=True)
class Embedder:
    model_pk: uuid.UUID
    provider: str
    model_id: str
    key: registry.ResolvedKey


class EmbedderUnavailable(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


async def embedder_for(session: AsyncSession, tenant_id: uuid.UUID, model_pk: uuid.UUID) -> Embedder:
    model = await session.get(AIModel, model_pk)
    if model is None or not model.active or model.kind != ModelKind.EMBEDDING:
        raise EmbedderUnavailable(
            "The chosen embedding model is no longer offered. Choose another in Knowledge settings.",
            "embedding_model_unavailable",
        )
    if model.provider not in registry.EMBEDDING_ADAPTERS:
        raise EmbedderUnavailable("This provider has no embeddings API.", "embedding_model_unavailable")
    configs = await registry.provider_configs(session, tenant_id)
    # The provider does not have to be switched on for replies, only have a usable key.
    key = await registry.resolve_key(session, tenant_id, configs.get(model.provider), require_enabled=False)
    if key is None:
        label = registry.PROVIDER_LABELS[ProviderKey(model.provider)]
        raise EmbedderUnavailable(
            f"{label} has no usable API key. Add one on the AI assistant page.", "embedding_key_missing"
        )
    return Embedder(model.id, model.provider, model.model_id, key)


class EmbedFailed(Exception):
    """A provider error, with the usage spent before it (to be recorded anyway)."""

    def __init__(self, error: ProviderError, call: CallRecord):
        super().__init__(str(error))
        self.error = error
        self.code = error.code
        self.call = call


@dataclass
class EmbedOutcome:
    vectors: list[list[float]]
    call: CallRecord


async def embed(embedder: Embedder, texts: list[str], *, task: EmbedTask, purpose: str) -> EmbedOutcome:
    """Embed in batches with retries on temporary errors. Raises EmbedFailed, which
    carries the usage spent so far so the caller can record it."""
    settings = get_settings()
    adapter = registry.build_embedder(embedder.provider, embedder.key.api_key)
    batch = max(1, settings.knowledge_embedding_batch)
    vectors: list[list[float]] = []
    usage = Usage()
    started = time.monotonic()

    def record(outcome: str) -> CallRecord:
        return CallRecord(
            provider=embedder.provider,
            model=embedder.model_id,
            key_source=embedder.key.key_source,
            usage=usage,
            outcome=outcome,
            purpose=purpose,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    for i in range(0, len(texts), batch):
        part = texts[i : i + batch]
        attempts = max(1, settings.ai_attempts_per_provider)
        for attempt in range(1, attempts + 1):
            try:
                result = await adapter.embed(
                    embedder.model_id, part, task=task, dimensions=EMBEDDING_DIMENSIONS
                )
                break
            except ProviderError as exc:
                if exc.retryable and attempt < attempts:
                    await asyncio.sleep(min(0.5 * attempt, 2.0))
                    continue
                raise EmbedFailed(exc, record(exc.code)) from exc
        for vector in result.vectors:
            if len(vector) != EMBEDDING_DIMENSIONS:
                error = ProviderBadRequest(
                    f"The model returned {len(vector)} dimensions instead of {EMBEDDING_DIMENSIONS}; "
                    "it can't be used for the knowledge base."
                )
                raise EmbedFailed(error, record(error.code))
        vectors += result.vectors
        usage.input_tokens += result.usage.input_tokens
    return EmbedOutcome(vectors, record("ok"))
