"""OpenAI through the Responses API (POST /v1/responses).

Structured output uses `text.format` with a strict JSON schema. Responses are not
stored at OpenAI (`store: false`): customer messages stay out of their 30-day store.
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


class OpenAIProvider:
    key = "openai"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def _client(self) -> httpx.AsyncClient:
        return http_client(
            get_settings().openai_base_url.rstrip("/"), {"Authorization": f"Bearer {self._api_key}"}
        )

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        body: dict[str, Any] = {
            "model": request.model,
            "instructions": request.system,
            "input": [{"role": t.role, "content": t.text} for t in merge_turns(request.turns)],
            "max_output_tokens": request.max_output_tokens,
            "store": False,
        }
        if request.response_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.response_schema.name,
                    "schema": request.response_schema.schema,
                    "strict": True,
                }
            }
        async with self._client() as client:
            data = await send(client, "POST", "/v1/responses", json=body)
        return parse_response(data, request.model)

    async def list_models(self) -> list[ModelInfo]:
        async with self._client() as client:
            data = await send(client, "GET", "/v1/models")
        return [ModelInfo(model_id=str(m["id"])) for m in data.get("data") or [] if m.get("id")]


def parse_response(data: dict[str, Any], model: str) -> GenerateResult:
    raw = data.get("usage") or {}
    usage = Usage(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cached_input_tokens=int((raw.get("input_tokens_details") or {}).get("cached_tokens") or 0),
    )
    texts: list[str] = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue  # reasoning items etc.
        for part in item.get("content") or []:
            if part.get("type") == "refusal":
                raise ProviderContentBlocked(str(part.get("refusal") or "The model refused."), usage=usage)
            if part.get("type") == "output_text":
                texts.append(str(part.get("text") or ""))
    status = data.get("status")
    if status == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason")
        if reason == "content_filter":
            raise ProviderContentBlocked("The provider's content filter stopped the answer.", usage=usage)
        raise ProviderIncomplete(f"The answer was cut off ({reason}).", usage=usage)
    if status == "failed":
        err = data.get("error") or {}
        raise ProviderIncomplete(str(err.get("message") or "The response failed."), usage=usage)
    text = "".join(texts).strip()
    if not text:
        raise ProviderIncomplete("The provider returned an empty answer.", usage=usage)
    return GenerateResult(
        text=text,
        usage=usage,
        model=str(data.get("model") or model),
        finish_reason=str(status) if status else None,
        raw_id=data.get("id"),
    )
