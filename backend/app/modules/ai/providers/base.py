"""The provider interface and one error scheme for every adapter.

Adapters speak each provider's documented REST API over httpx (no vendor SDKs), so
tests can swap the transport and every call has an explicit timeout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

from app.core.config import get_settings

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class Turn:
    role: Role
    text: str


@dataclass(frozen=True)
class JsonSchema:
    """A structured-output schema in plain JSON Schema (object, string, boolean)."""

    name: str
    schema: dict[str, Any]


@dataclass(frozen=True)
class GenerateRequest:
    model: str
    system: str
    turns: list[Turn]
    max_output_tokens: int
    response_schema: JsonSchema | None = None
    temperature: float | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


@dataclass
class GenerateResult:
    text: str
    usage: Usage
    model: str
    finish_reason: str | None = None
    raw_id: str | None = None


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """Base error. `retryable` errors may succeed on the same provider later;
    every error moves the orchestrator on to the next provider in the chain."""

    code = "provider_error"
    retryable = False

    def __init__(self, message: str, *, status: int | None = None, usage: Usage | None = None):
        super().__init__(message)
        self.status = status
        self.usage = usage  # tokens may be billed even when the answer is unusable


class ProviderTimeout(ProviderError):
    code = "provider_timeout"
    retryable = True


class ProviderRateLimited(ProviderError):
    code = "provider_rate_limited"
    retryable = True


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"
    retryable = True


class ProviderAuthError(ProviderError):
    code = "provider_auth_error"


class ProviderBillingError(ProviderError):
    code = "provider_billing_error"


class ProviderContentBlocked(ProviderError):
    code = "provider_content_blocked"


class ProviderBadRequest(ProviderError):
    code = "provider_bad_request"


class ProviderIncomplete(ProviderError):
    """The answer was cut off (output limit) or empty."""

    code = "provider_incomplete"


class AIProvider(Protocol):
    key: str

    async def generate(self, request: GenerateRequest) -> GenerateResult: ...

    async def list_models(self) -> list[ModelInfo]: ...


# ---- shared HTTP plumbing ------------------------------------------------------

_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport_override(transport: httpx.AsyncBaseTransport | None) -> None:
    """Route every provider request through `transport` (tests use a fake)."""
    global _transport_override
    _transport_override = transport


def http_client(base_url: str, headers: dict[str, str]) -> httpx.AsyncClient:
    timeout = get_settings().ai_http_timeout_seconds
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        transport=_transport_override,
    )


def error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text[:300] or f"HTTP {response.status_code}"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("status") or err)[:300]
    if isinstance(err, str):
        return err[:300]
    return str(data)[:300]


def raise_for_status(response: httpx.Response) -> None:
    """Map an HTTP error to the shared taxonomy."""
    if response.is_success:
        return
    status = response.status_code
    message = error_message(response)
    if status in (401, 403):
        raise ProviderAuthError(message, status=status)
    if status == 402:
        raise ProviderBillingError(message, status=status)
    if status == 429:
        raise ProviderRateLimited(message, status=status)
    if status == 408 or status >= 500:
        raise ProviderUnavailable(message, status=status)
    raise ProviderBadRequest(message, status=status)


async def send(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise ProviderTimeout("The AI provider did not answer in time.") from exc
    except httpx.HTTPError as exc:
        raise ProviderUnavailable(f"Could not reach the AI provider: {type(exc).__name__}") from exc
    raise_for_status(response)
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderUnavailable("The AI provider returned an unreadable response.") from exc
    if not isinstance(data, dict):
        raise ProviderUnavailable("The AI provider returned an unexpected response.")
    return data


def merge_turns(turns: list[Turn]) -> list[Turn]:
    """Join consecutive turns from the same side; some APIs expect strict alternation."""
    merged: list[Turn] = []
    for turn in turns:
        if merged and merged[-1].role == turn.role:
            merged[-1] = Turn(turn.role, f"{merged[-1].text}\n{turn.text}")
        else:
            merged.append(turn)
    return merged
