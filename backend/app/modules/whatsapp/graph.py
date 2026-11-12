"""Async client for Meta's Graph API (WhatsApp Cloud API and Business Management).

Only official, documented endpoints are used. Every Meta error is mapped to
GraphError with a `retryable` flag so callers decide between retry and failure
without parsing Meta codes themselves.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from app.core.config import get_settings

log = structlog.get_logger(__name__)

# Meta error codes (https://developers.facebook.com/documentation/business-messaging/whatsapp/support/error-codes)
TOKEN_ERROR_CODES = {190, 102, 463, 467}  # invalid / expired / revoked token
PERMISSION_ERROR_CODES = {10, 200, 299}
RATE_LIMIT_CODES = {4, 17, 32, 80007, 130429, 131048, 131056, 133016}
TRANSIENT_CODES = {1, 2, 131000, 131016, 133004}
OUTSIDE_WINDOW_CODE = 131047
RECIPIENT_UNAVAILABLE_CODE = 131026
PAYMENT_ERROR_CODES = {131042}


@dataclass
class GraphError(Exception):
    message: str
    status: int = 0
    code: int | None = None
    subcode: int | None = None
    error_type: str | None = None
    fbtrace_id: str | None = None
    details: str | None = None
    retryable: bool = False

    def __str__(self) -> str:
        return f"Graph API error {self.code or self.status}: {self.message}"

    @property
    def is_token_error(self) -> bool:
        return self.code in TOKEN_ERROR_CODES

    @property
    def is_permission_error(self) -> bool:
        return self.code in PERMISSION_ERROR_CODES

    def public(self) -> dict[str, Any]:
        """Safe to store or show: no tokens, no request data."""
        return {
            "code": self.code,
            "subcode": self.subcode,
            "message": self.message[:300],
            "details": (self.details or "")[:300] or None,
            "fbtrace_id": self.fbtrace_id,
        }


def _parse_error(response: httpx.Response) -> GraphError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    err = body.get("error", {}) if isinstance(body, dict) else {}
    code = err.get("code")
    retryable = response.status_code >= 500 or code in RATE_LIMIT_CODES or code in TRANSIENT_CODES
    if response.status_code == 429:
        retryable = True
    error_data = err.get("error_data") or {}
    return GraphError(
        message=str(err.get("message") or response.reason_phrase or "Graph API error"),
        status=response.status_code,
        code=code,
        subcode=err.get("error_subcode"),
        error_type=err.get("type"),
        fbtrace_id=err.get("fbtrace_id"),
        details=error_data.get("details") if isinstance(error_data, dict) else None,
        retryable=retryable,
    )


@dataclass
class TokenInfo:
    is_valid: bool
    app_id: str | None
    scopes: list[str]
    granular_scopes: list[dict[str, Any]] = field(default_factory=list)
    expires_at: int | None = None
    user_id: str | None = None

    def targets_for(self, scope: str) -> set[str]:
        for item in self.granular_scopes:
            if item.get("scope") == scope:
                return {str(t) for t in item.get("target_ids") or []}
        return set()


class GraphClient:
    """One client per call site; reuse inside a unit of work. Tokens are passed per
    request so a client never holds a tenant's secret longer than needed."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        settings = get_settings()
        self.version = settings.meta_graph_api_version
        self.app_id = settings.meta_app_id
        self._app_secret = settings.meta_app_secret.get_secret_value() if settings.meta_app_secret else None
        self._client = httpx.AsyncClient(
            base_url=f"{settings.meta_graph_base_url.rstrip('/')}/{self.version}",
            timeout=httpx.Timeout(settings.meta_http_timeout_seconds, connect=5.0),
            transport=transport or _transport_override,
            headers={"User-Agent": "XploWhatsApp/1.0"},
        )

    async def __aenter__(self) -> GraphClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = await self._client.request(method, path, params=params, json=json, headers=headers)
        except httpx.TimeoutException as exc:
            raise GraphError("Timed out talking to Meta.", retryable=True) from exc
        except httpx.TransportError as exc:
            raise GraphError("Could not reach Meta.", retryable=True) from exc
        if response.status_code >= 400:
            error = _parse_error(response)
            log.warning(
                "graph_api_error",
                path=path.split("?")[0],
                status=error.status,
                code=error.code,
                subcode=error.subcode,
                fbtrace_id=error.fbtrace_id,
            )
            raise error
        try:
            data = response.json()
        except ValueError as exc:
            raise GraphError("Meta returned an unreadable response.", status=response.status_code) from exc
        return data if isinstance(data, dict) else {"data": data}

    def _require_app_credentials(self) -> tuple[str, str]:
        if not self.app_id or not self._app_secret:
            raise GraphError("META_APP_ID and META_APP_SECRET are not configured.")
        return self.app_id, self._app_secret

    # ---- Onboarding ----------------------------------------------------------

    async def exchange_code(self, code: str) -> str:
        """Exchange the Embedded Signup code for a business integration system user token."""
        app_id, secret = self._require_app_credentials()
        data = await self._request(
            "GET", "/oauth/access_token", params={"client_id": app_id, "client_secret": secret, "code": code}
        )
        token = data.get("access_token")
        if not token:
            raise GraphError("Meta did not return an access token.")
        return str(token)

    async def debug_token(self, token: str) -> TokenInfo:
        app_id, secret = self._require_app_credentials()
        data = await self._request(
            "GET", "/debug_token", params={"input_token": token, "access_token": f"{app_id}|{secret}"}
        )
        info = data.get("data", {})
        return TokenInfo(
            is_valid=bool(info.get("is_valid")),
            app_id=str(info["app_id"]) if info.get("app_id") else None,
            scopes=list(info.get("scopes") or []),
            granular_scopes=list(info.get("granular_scopes") or []),
            expires_at=info.get("expires_at") or None,
            user_id=str(info["user_id"]) if info.get("user_id") else None,
        )

    async def get_waba(self, token: str, waba_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/{waba_id}",
            token=token,
            params={"fields": "id,name,currency,timezone_id,message_template_namespace,owner_business_info"},
        )

    async def list_phone_numbers(self, token: str, waba_id: str) -> list[dict[str, Any]]:
        fields = (
            "id,display_phone_number,verified_name,quality_rating,code_verification_status,"
            "name_status,platform_type,status,messaging_limit_tier,throughput"
        )
        data = await self._request("GET", f"/{waba_id}/phone_numbers", token=token, params={"fields": fields})
        return list(data.get("data") or [])

    async def subscribe_app(self, token: str, waba_id: str) -> None:
        await self._request("POST", f"/{waba_id}/subscribed_apps", token=token)

    async def unsubscribe_app(self, token: str, waba_id: str) -> None:
        await self._request("DELETE", f"/{waba_id}/subscribed_apps", token=token)

    async def list_subscribed_apps(self, token: str, waba_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/{waba_id}/subscribed_apps", token=token)
        return list(data.get("data") or [])

    async def register_number(self, token: str, phone_number_id: str, pin: str) -> None:
        await self._request(
            "POST",
            f"/{phone_number_id}/register",
            token=token,
            json={"messaging_product": "whatsapp", "pin": pin},
        )

    # ---- Messaging -----------------------------------------------------------

    async def send_message(self, token: str, phone_number_id: str, body: dict[str, Any]) -> str:
        data = await self._request(
            "POST",
            f"/{phone_number_id}/messages",
            token=token,
            json={"messaging_product": "whatsapp", **body},
        )
        messages = data.get("messages") or []
        if not messages or not messages[0].get("id"):
            raise GraphError("Meta accepted the request but returned no message ID.")
        return str(messages[0]["id"])

    # ---- Templates -----------------------------------------------------------

    async def iter_templates(
        self, token: str, waba_id: str, page_size: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "fields": "id,name,language,status,category,components,rejected_reason",
            "limit": page_size,
        }
        after: str | None = None
        for _ in range(100):  # hard stop: 10,000 templates
            if after:
                params["after"] = after
            data = await self._request("GET", f"/{waba_id}/message_templates", token=token, params=params)
            for item in data.get("data") or []:
                yield item
            paging = data.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not paging.get("next") or not after:
                return


# Tests (and local tooling) can route every GraphClient through a fake transport.
_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport_override(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport_override
    _transport_override = transport
