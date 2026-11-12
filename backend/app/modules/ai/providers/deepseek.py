"""DeepSeek through its OpenAI-compatible Chat Completions endpoint.

DeepSeek offers JSON mode (`response_format: json_object`) rather than schema-enforced
output, and its docs require the word "json" in the prompt; the orchestrator's system
prompt describes the JSON shape, and the validator checks it.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import get_settings
from app.modules.ai.providers.base import (
    GenerateRequest,
    GenerateResult,
    ModelInfo,
    ProviderContentBlocked,
    ProviderIncomplete,
    Usage,
    http_client,
    merge_turns,
    send,
)


class DeepSeekProvider:
    key = "deepseek"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def _client(self) -> httpx.AsyncClient:
        return http_client(
            get_settings().deepseek_base_url.rstrip("/"), {"Authorization": f"Bearer {self._api_key}"}
        )

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        messages: list[dict[str, str]] = [{"role": "system", "content": request.system}]
        messages += [{"role": t.role, "content": t.text} for t in merge_turns(request.turns)]
        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.response_schema is not None:
            body["response_format"] = {"type": "json_object"}
        async with self._client() as client:
            data = await send(client, "POST", "/chat/completions", json=body)
        return parse_response(data, request.model)

    async def list_models(self) -> list[ModelInfo]:
        async with self._client() as client:
            data = await send(client, "GET", "/models")
        return [ModelInfo(model_id=str(m["id"])) for m in data.get("data") or [] if m.get("id")]


def parse_response(data: dict[str, Any], model: str) -> GenerateResult:
    raw = data.get("usage") or {}
    usage = Usage(
        input_tokens=int(raw.get("prompt_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cached_input_tokens=int(raw.get("prompt_cache_hit_tokens") or 0),
    )
    choices = data.get("choices") or []
    if not choices:
        raise ProviderIncomplete("The provider returned no answer.", usage=usage)
    choice = choices[0]
    reason = choice.get("finish_reason")
    if reason == "content_filter":
        raise ProviderContentBlocked("The provider's content filter stopped the answer.", usage=usage)
    if reason == "length":
        raise ProviderIncomplete("The answer was cut off (max tokens).", usage=usage)
    text = str((choice.get("message") or {}).get("content") or "").strip()
    if not text:
        raise ProviderIncomplete("The provider returned an empty answer.", usage=usage)
    return GenerateResult(
        text=text,
        usage=usage,
        model=str(data.get("model") or model),
        finish_reason=reason,
        raw_id=data.get("id"),
    )
