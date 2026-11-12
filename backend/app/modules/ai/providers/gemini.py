"""Google Gemini API (generativelanguage.googleapis.com, models.generateContent).

The key goes in the `x-goog-api-key` header, never in the URL, so it cannot end up
in access logs. Structured output uses responseMimeType + responseSchema (an OpenAPI
subset: upper-case type names, no additionalProperties).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.modules.ai.providers.base import (
    GenerateRequest,
    GenerateResult,
    ModelInfo,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderContentBlocked,
    ProviderIncomplete,
    Usage,
    http_client,
    merge_turns,
    send,
)

BLOCKED_REASONS = {
    "SAFETY",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "RECITATION",
    "IMAGE_SAFETY",
    "OTHER",
}


class GeminiProvider:
    key = "gemini"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def _client(self) -> httpx.AsyncClient:
        return http_client(get_settings().gemini_base_url.rstrip("/"), {"x-goog-api-key": self._api_key})

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        config: dict[str, Any] = {"maxOutputTokens": request.max_output_tokens}
        if request.temperature is not None:
            config["temperature"] = request.temperature
        if request.response_schema is not None:
            config["responseMimeType"] = "application/json"
            config["responseSchema"] = to_gemini_schema(request.response_schema.schema)
        body = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [
                {"role": "model" if t.role == "assistant" else "user", "parts": [{"text": t.text}]}
                for t in merge_turns(request.turns)
            ],
            "generationConfig": config,
        }
        model = request.model.removeprefix("models/")
        async with self._client() as client:
            try:
                data = await send(
                    client, "POST", f"/v1beta/models/{quote(model, safe='-._')}:generateContent", json=body
                )
            except ProviderBadRequest as exc:
                # An invalid key comes back as 400 INVALID_ARGUMENT.
                if "api key" in str(exc).lower():
                    raise ProviderAuthError(str(exc), status=exc.status) from exc
                raise
        return parse_response(data, request.model)

    async def list_models(self) -> list[ModelInfo]:
        models: list[ModelInfo] = []
        page_token: str | None = None
        async with self._client() as client:
            for _ in range(20):  # bounded pagination
                params: dict[str, Any] = {"pageSize": 1000}
                if page_token:
                    params["pageToken"] = page_token
                data = await send(client, "GET", "/v1beta/models", params=params)
                for m in data.get("models") or []:
                    methods = m.get("supportedGenerationMethods") or []
                    if "generateContent" not in methods:
                        continue
                    models.append(
                        ModelInfo(
                            model_id=str(m.get("name", "")).removeprefix("models/"),
                            display_name=m.get("displayName"),
                            context_window=m.get("inputTokenLimit"),
                            max_output_tokens=m.get("outputTokenLimit"),
                            capabilities={"thinking": bool(m.get("thinking"))} if "thinking" in m else {},
                        )
                    )
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        return [m for m in models if m.model_id]


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if key == "type" and isinstance(value, str):
            out[key] = value.upper()
        elif key == "properties" and isinstance(value, dict):
            out[key] = {k: to_gemini_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[key] = to_gemini_schema(value)
        else:
            out[key] = value
    return out


def parse_response(data: dict[str, Any], model: str) -> GenerateResult:
    raw = data.get("usageMetadata") or {}
    usage = Usage(
        input_tokens=int(raw.get("promptTokenCount") or 0),
        # Thinking tokens are billed as output.
        output_tokens=int(raw.get("candidatesTokenCount") or 0) + int(raw.get("thoughtsTokenCount") or 0),
        cached_input_tokens=int(raw.get("cachedContentTokenCount") or 0),
    )
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise ProviderContentBlocked(f"Blocked by the provider ({feedback['blockReason']}).", usage=usage)
    candidates = data.get("candidates") or []
    if not candidates:
        raise ProviderIncomplete("The provider returned no answer.", usage=usage)
    candidate = candidates[0]
    reason = candidate.get("finishReason")
    if reason in BLOCKED_REASONS:
        raise ProviderContentBlocked(f"Blocked by the provider ({reason}).", usage=usage)
    if reason == "MAX_TOKENS":
        raise ProviderIncomplete("The answer was cut off (max tokens).", usage=usage)
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(str(p.get("text") or "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise ProviderIncomplete("The provider returned an empty answer.", usage=usage)
    return GenerateResult(
        text=text,
        usage=usage,
        model=str(data.get("modelVersion") or model),
        finish_reason=reason,
        raw_id=data.get("responseId"),
    )
