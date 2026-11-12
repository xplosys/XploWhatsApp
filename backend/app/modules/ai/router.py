"""AI settings for a business: providers and keys, the assistant, preview and usage."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import Meta, Requires, TenantContext
from app.core.errors import NotFound, ValidationFailed
from app.core.rate_limit import hit
from app.modules.ai import catalogue, orchestrator, registry
from app.modules.ai.assistant import (
    AssistantConfig,
    active_config,
    get_assistant,
    get_version,
    list_versions,
    save_version,
)
from app.modules.ai.models import (
    AIProviderConfig,
    Assistant,
    AssistantVersion,
    KeySource,
    ProviderKey,
    UsageRecord,
)
from app.modules.ai.providers.base import ProviderError, Turn
from app.modules.ai.schemas import (
    AssistantOut,
    AssistantSave,
    AssistantToggle,
    AttemptOut,
    ModelOut,
    PreviewIn,
    PreviewOut,
    ProviderOut,
    ProviderUpdate,
    UsageOut,
    UsageRow,
    VersionOut,
)
from app.modules.audit.service import record
from app.modules.billing.service import effective_plan
from app.modules.rbac.permissions import Permission

router = APIRouter(prefix="/ai", tags=["ai"])

Configurer = Annotated[TenantContext, Requires(Permission.AI_CONFIGURE)]

PREVIEW_PER_MINUTE = 20


async def _provider_out(
    ctx: TenantContext, provider: ProviderKey, config: AIProviderConfig | None
) -> ProviderOut:
    allowed = await registry.platform_keys_allowed(ctx.session, ctx.tenant_id)
    platform = bool(registry.platform_key(provider)) and allowed
    ready = (await registry.resolve_key(ctx.session, ctx.tenant_id, config)) is not None
    return ProviderOut(
        provider=provider,
        label=registry.PROVIDER_LABELS[provider],
        enabled=bool(config and config.enabled),
        key_source=KeySource(config.key_source) if config else KeySource.TENANT,
        has_own_key=bool(config and config.api_key_ciphertext),
        key_hint=config.key_hint if config else None,
        platform_key_available=platform,
        ready=ready,
        last_test_at=config.last_test_at if config else None,
        last_test_ok=config.last_test_ok if config else None,
        last_error=config.last_error if config else None,
    )


async def _config_for(
    ctx: TenantContext, provider: ProviderKey, *, create: bool = False
) -> AIProviderConfig | None:
    config = (
        await ctx.session.execute(
            select(AIProviderConfig)
            .where(AIProviderConfig.tenant_id == ctx.tenant_id, AIProviderConfig.provider == provider)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if config is None and create:
        config = AIProviderConfig(
            tenant_id=ctx.tenant_id, provider=provider, enabled=False, key_source=KeySource.TENANT
        )
        ctx.session.add(config)
    return config


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(ctx: Configurer) -> list[ProviderOut]:
    configs = await registry.provider_configs(ctx.session, ctx.tenant_id)
    return [await _provider_out(ctx, p, configs.get(p)) for p in ProviderKey]


@router.put("/providers/{provider}", response_model=ProviderOut)
async def update_provider(
    provider: ProviderKey, body: ProviderUpdate, ctx: Configurer, meta: Meta
) -> ProviderOut:
    config = await _config_for(ctx, provider, create=True)
    assert config is not None
    if body.key_source == KeySource.PLATFORM:
        if not registry.platform_key(provider):
            raise ValidationFailed(
                "XploSys has no key for this provider. Add your own key.", code="no_platform_key"
            )
        if not await registry.platform_keys_allowed(ctx.session, ctx.tenant_id):
            raise ValidationFailed(
                "Your plan does not include XploSys AI keys. Add your own key.", code="plan_limit"
            )
    key_changed = body.api_key is not None
    if body.api_key is not None:
        registry.set_tenant_key(config, body.api_key.get_secret_value().strip())
        config.last_test_at = config.last_test_ok = config.last_error = None
    if body.enabled and body.key_source == KeySource.TENANT and not config.api_key_ciphertext:
        raise ValidationFailed("Add an API key before turning this provider on.", code="api_key_required")
    config.enabled = body.enabled
    config.key_source = body.key_source
    config.updated_by_user_id = ctx.user.id
    await ctx.session.flush()
    await record(
        ctx.session,
        "ai.provider_updated",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="ai_provider",
        target_id=provider.value,
        details={"enabled": body.enabled, "key_source": body.key_source.value, "key_changed": key_changed},
    )
    return await _provider_out(ctx, provider, config)


@router.delete("/providers/{provider}/key", response_model=ProviderOut)
async def delete_provider_key(provider: ProviderKey, ctx: Configurer, meta: Meta) -> ProviderOut:
    config = await _config_for(ctx, provider)
    if config is None or not config.api_key_ciphertext:
        raise NotFound("No key is stored for this provider.")
    registry.clear_tenant_key(config)
    if config.key_source == KeySource.TENANT:
        config.enabled = False
    await ctx.session.flush()
    await record(
        ctx.session,
        "ai.provider_key_deleted",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="ai_provider",
        target_id=provider.value,
    )
    return await _provider_out(ctx, provider, config)


@router.post("/providers/{provider}/test", response_model=ProviderOut)
async def test_provider(provider: ProviderKey, ctx: Configurer) -> ProviderOut:
    """Checks the key by listing the provider's models (no tokens are used)."""
    config = await _config_for(ctx, provider)
    key = await registry.resolve_key(ctx.session, ctx.tenant_id, config, require_enabled=False)
    if config is None or key is None:
        raise ValidationFailed("Add an API key for this provider first.", code="api_key_required")
    try:
        await registry.build(provider, key.api_key).list_models()
        config.last_test_ok, config.last_error = True, None
    except ProviderError as exc:
        config.last_test_ok, config.last_error = False, f"{exc.code}: {exc}"[:500]
    config.last_test_at = datetime.now(UTC)
    await ctx.session.flush()
    return await _provider_out(ctx, provider, config)


@router.get("/models", response_model=list[ModelOut])
async def list_models(ctx: Configurer, provider: ProviderKey | None = None) -> list[ModelOut]:
    return [ModelOut.model_validate(m) for m in await catalogue.active_models(ctx.session, provider)]


# ---- assistant ----------------------------------------------------------------------


def _version_out(v: AssistantVersion) -> VersionOut:
    return VersionOut(
        version=v.version, config=v.config, created_at=v.created_at, created_by_user_id=v.created_by_user_id
    )


async def _warnings(ctx: TenantContext, assistant: Assistant) -> list[str]:
    config = await active_config(ctx.session, assistant)
    if config is None:
        return ["Save the assistant's settings first."]
    warnings = []
    steps = await orchestrator.prepare_chain(ctx.session, ctx.tenant_id, config)
    for step in steps:
        if step.skip_reason == "model_unavailable":
            warnings.append(f"{step.provider} model {step.model} is no longer offered; choose another.")
        elif step.skip_reason == "provider_not_ready":
            warnings.append(f"{registry.PROVIDER_LABELS[ProviderKey(step.provider)]} is off or has no key.")
    if all(s.key is None for s in steps):
        warnings.append("No model in the chain can answer yet, so automatic replies cannot run.")
    if not (config.instructions or config.business_description):
        warnings.append("Add some business information; without it the assistant can only hand over.")
    return warnings


async def _assistant_out(ctx: TenantContext, assistant: Assistant) -> AssistantOut:
    version = (
        await get_version(ctx.session, assistant, assistant.active_version)
        if assistant.active_version
        else None
    )
    return AssistantOut(
        id=assistant.id,
        name=assistant.name,
        auto_reply_enabled=assistant.auto_reply_enabled,
        active_version=_version_out(version) if version else None,
        warnings=await _warnings(ctx, assistant),
    )


async def _require_assistant(ctx: TenantContext) -> Assistant:
    assistant = await get_assistant(ctx.session, ctx.tenant_id, lock=True)
    if assistant is None:
        raise NotFound("Set up the assistant first.", code="assistant_not_found")
    return assistant


async def _check_models(ctx: TenantContext, config: AssistantConfig) -> None:
    for choice in config.chain:
        model = await catalogue.find_model(ctx.session, choice.provider, choice.model)
        if model is None or not model.active:
            raise ValidationFailed(
                f"{choice.model} is not an available {choice.provider} model.", code="model_unavailable"
            )


@router.get("/assistant", response_model=AssistantOut | None)
async def get_assistant_settings(ctx: Configurer) -> AssistantOut | None:
    assistant = await get_assistant(ctx.session, ctx.tenant_id)
    return await _assistant_out(ctx, assistant) if assistant else None


@router.put("/assistant", response_model=AssistantOut)
async def save_assistant(body: AssistantSave, ctx: Configurer, meta: Meta) -> AssistantOut:
    await _check_models(ctx, body.config)
    assistant, version = await save_version(
        ctx.session, ctx.tenant_id, body.config, name=body.name, user_id=ctx.user.id
    )
    await record(
        ctx.session,
        "ai.assistant_saved",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="assistant",
        target_id=assistant.id,
        details={"version": version.version},
    )
    return await _assistant_out(ctx, assistant)


@router.patch("/assistant", response_model=AssistantOut)
async def toggle_assistant(body: AssistantToggle, ctx: Configurer, meta: Meta) -> AssistantOut:
    assistant = await _require_assistant(ctx)
    if body.auto_reply_enabled:
        config = await active_config(ctx.session, assistant)
        if config is None:
            raise ValidationFailed("Save the assistant's settings first.", code="assistant_not_ready")
        steps = await orchestrator.prepare_chain(ctx.session, ctx.tenant_id, config)
        if all(s.key is None for s in steps):
            raise ValidationFailed(
                "Turn on a provider with a working key for at least one model in the chain.",
                code="assistant_not_ready",
            )
    assistant.auto_reply_enabled = body.auto_reply_enabled
    await record(
        ctx.session,
        "ai.auto_reply_enabled" if body.auto_reply_enabled else "ai.auto_reply_disabled",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="assistant",
        target_id=assistant.id,
    )
    return await _assistant_out(ctx, assistant)


@router.get("/assistant/versions", response_model=list[VersionOut])
async def assistant_versions(ctx: Configurer) -> list[VersionOut]:
    assistant = await get_assistant(ctx.session, ctx.tenant_id)
    return [_version_out(v) for v in await list_versions(ctx.session, assistant)] if assistant else []


@router.post("/assistant/versions/{version}/activate", response_model=AssistantOut)
async def activate_version(version: int, ctx: Configurer, meta: Meta) -> AssistantOut:
    assistant = await _require_assistant(ctx)
    row = await get_version(ctx.session, assistant, version)
    await _check_models(ctx, AssistantConfig.model_validate(row.config))
    assistant.active_version = row.version
    await record(
        ctx.session,
        "ai.assistant_version_activated",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="assistant",
        target_id=assistant.id,
        details={"version": version},
    )
    return await _assistant_out(ctx, assistant)


@router.post("/assistant/preview", response_model=PreviewOut)
async def preview(body: PreviewIn, ctx: Configurer) -> PreviewOut:
    """Ask the assistant something without sending anything to WhatsApp. Uses real
    provider calls, so it is metered and rate limited."""
    await hit(f"ai_preview:{ctx.tenant_id}", PREVIEW_PER_MINUTE)
    config = body.config
    if config is None:
        assistant = await get_assistant(ctx.session, ctx.tenant_id)
        config = await active_config(ctx.session, assistant) if assistant else None
        if config is None:
            raise ValidationFailed("Save the assistant's settings first.", code="assistant_not_ready")
    await _check_models(ctx, config)
    if body.messages[-1].role != "user":
        raise ValidationFailed("The last message must be from the customer.", code="last_turn_not_user")
    steps = await orchestrator.prepare_chain(ctx.session, ctx.tenant_id, config)
    outcome = await orchestrator.run(
        steps,
        config,
        business_name=ctx.tenant.name,
        default_language=ctx.tenant.default_language or "en",
        turns=[Turn(m.role, m.text) for m in body.messages],
    )
    for call in orchestrator.call_records(outcome, purpose="preview"):
        await catalogue.record_call(ctx.session, ctx.tenant_id, call)
    return PreviewOut(
        kind=outcome.kind,
        text=outcome.text,
        reason=outcome.reason,
        provider=outcome.provider,
        model=outcome.model,
        out_of_scope=outcome.out_of_scope,
        attempts=[
            AttemptOut(provider=a.provider, model=a.model, outcome=a.outcome, detail=a.detail)
            for a in outcome.attempts
        ],
    )


# ---- usage ----------------------------------------------------------------------------


@router.get("/usage", response_model=UsageOut)
async def usage(
    ctx: Configurer, month: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None
) -> UsageOut:
    if month:
        start = datetime(int(month[:4]), int(month[5:]), 1, tzinfo=UTC)
    else:
        start = catalogue.month_start()
    end = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1, tzinfo=UTC)
    records = (
        await ctx.session.execute(
            select(UsageRecord).where(
                UsageRecord.tenant_id == ctx.tenant_id,
                UsageRecord.occurred_at >= start,
                UsageRecord.occurred_at < end,
            )
        )
    ).scalars()
    replies = 0
    rows: dict[tuple[str, str], UsageRow] = {}
    for r in records:
        if r.metric == "ai_reply":
            replies += 1
            continue
        if r.metric != "ai_call":
            continue
        d = r.dimensions
        key = (str(d.get("provider")), str(d.get("model")))
        row = rows.setdefault(
            key,
            UsageRow(
                provider=key[0],
                model=key[1],
                calls=0,
                failed_calls=0,
                input_tokens=0,
                output_tokens=0,
                cost_estimate=None,
                currency=None,
                unpriced_calls=0,
            ),
        )
        row.calls += 1
        row.failed_calls += d.get("outcome") != "ok"
        row.input_tokens += int(d.get("input_tokens") or 0)
        row.output_tokens += int(d.get("output_tokens") or 0)
        if r.cost_source == "unpriced":
            row.unpriced_calls += 1
        if r.cost_estimate is not None:
            if row.currency in (None, r.currency):
                row.currency = r.currency
                row.cost_estimate = (row.cost_estimate or Decimal(0)) + r.cost_estimate
            else:
                row.unpriced_calls += 1  # mixed currencies are not summed
    plan = await effective_plan(ctx.session, ctx.tenant_id)
    return UsageOut(
        period_start=start,
        ai_replies_used=replies,
        ai_replies_limit=plan.limit("monthly_ai_messages", 0),
        platform_keys_allowed=await registry.platform_keys_allowed(ctx.session, ctx.tenant_id),
        rows=sorted(rows.values(), key=lambda r: (r.provider, r.model)),
    )
