"""Runs the assistant: picks providers, retries, falls back, and validates answers.

Preparing the chain needs the database (keys, catalogue); running it does not, so the
caller can hold no transaction open while the providers think.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.ai import catalogue, registry
from app.modules.ai.assistant import AssistantConfig
from app.modules.ai.catalogue import CallRecord
from app.modules.ai.prompt import REPLY_SCHEMA, grounding_text, system_prompt
from app.modules.ai.providers.base import GenerateRequest, ProviderError, Turn, Usage
from app.modules.ai.validator import validate


@dataclass(frozen=True)
class ChainStep:
    provider: str
    model: str
    key: registry.ResolvedKey | None
    skip_reason: str | None = None


@dataclass
class Attempt:
    provider: str
    model: str
    outcome: str  # ok | invalid | skipped | <error code>
    detail: str | None = None
    usage: Usage = field(default_factory=Usage)
    key_source: str | None = None
    latency_ms: int = 0


@dataclass
class Outcome:
    kind: Literal["reply", "handover"]
    text: str
    reason: str  # answered, needs_human, all_failed, ...
    provider: str | None = None
    model: str | None = None
    out_of_scope: bool = False
    attempts: list[Attempt] = field(default_factory=list)

    def provenance(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "out_of_scope": self.out_of_scope,
            "attempts": [
                {"provider": a.provider, "model": a.model, "outcome": a.outcome, "detail": a.detail}
                for a in self.attempts
            ],
        }


async def prepare_chain(
    session: AsyncSession, tenant_id: uuid.UUID, config: AssistantConfig
) -> list[ChainStep]:
    configs = await registry.provider_configs(session, tenant_id)
    steps: list[ChainStep] = []
    for choice in config.chain:
        provider = str(choice.provider)
        model = await catalogue.find_model(session, provider, choice.model)
        if model is None or not model.active:
            steps.append(ChainStep(provider, choice.model, None, "model_unavailable"))
            continue
        key = await registry.resolve_key(session, tenant_id, configs.get(provider))
        steps.append(ChainStep(provider, choice.model, key, None if key else "provider_not_ready"))
    return steps


async def run(
    steps: list[ChainStep],
    config: AssistantConfig,
    *,
    business_name: str,
    default_language: str,
    turns: list[Turn],
) -> Outcome:
    settings = get_settings()
    request_base = GenerateRequest(
        model="",
        system=system_prompt(config, business_name=business_name, default_language=default_language),
        turns=turns,
        max_output_tokens=settings.ai_max_output_tokens,
        response_schema=REPLY_SCHEMA,
    )
    grounding = grounding_text(config, turns)
    attempts: list[Attempt] = []
    for step in steps:
        if step.key is None:
            attempts.append(Attempt(step.provider, step.model, "skipped", step.skip_reason))
            continue
        adapter = registry.build(step.provider, step.key.api_key)
        request = GenerateRequest(**{**request_base.__dict__, "model": step.model})
        for attempt_no in range(1, max(1, settings.ai_attempts_per_provider) + 1):
            started = time.monotonic()
            try:
                result = await adapter.generate(request)
            except ProviderError as exc:
                attempts.append(
                    Attempt(
                        step.provider,
                        step.model,
                        exc.code,
                        str(exc)[:300],
                        exc.usage or Usage(),
                        step.key.key_source,
                        int((time.monotonic() - started) * 1000),
                    )
                )
                if exc.retryable and attempt_no < settings.ai_attempts_per_provider:
                    await asyncio.sleep(min(0.5 * attempt_no, 2.0))
                    continue
                break
            latency = int((time.monotonic() - started) * 1000)
            verdict = validate(result.text, max_chars=config.max_reply_chars, grounding=grounding)
            if verdict.kind == "invalid":
                attempts.append(
                    Attempt(
                        step.provider,
                        step.model,
                        "invalid",
                        "; ".join(verdict.issues),
                        result.usage,
                        step.key.key_source,
                        latency,
                    )
                )
                break  # a different model may do better; this one already had its chance
            attempts.append(
                Attempt(step.provider, step.model, "ok", None, result.usage, step.key.key_source, latency)
            )
            if verdict.kind == "handover":
                return Outcome(
                    "handover",
                    verdict.text or config.handover_message,
                    "needs_human",
                    step.provider,
                    step.model,
                    attempts=attempts,
                )
            return Outcome(
                "reply", verdict.text, "answered", step.provider, step.model, verdict.out_of_scope, attempts
            )
    return Outcome("handover", config.handover_message, "all_failed", attempts=attempts)


def call_records(
    outcome: Outcome, *, purpose: str, conversation_id: uuid.UUID | None = None
) -> list[CallRecord]:
    return [
        CallRecord(
            provider=a.provider,
            model=a.model,
            key_source=a.key_source or "",
            usage=a.usage,
            outcome=a.outcome,
            purpose=purpose,
            latency_ms=a.latency_ms,
            conversation_id=conversation_id,
        )
        for a in outcome.attempts
        if a.outcome != "skipped"
    ]
