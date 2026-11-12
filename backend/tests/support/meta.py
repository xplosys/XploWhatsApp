"""A fake Graph API for tests. It answers the documented endpoints XploWhatsApp
uses with Meta-shaped responses, records every request, and can be told to fail."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.modules.webhooks.signature import compute_signature

WABA_ID = "102290129340398"
PHONE_NUMBER_ID = "106540352242922"
DISPLAY_PHONE = "+91 98765 43210"
BUSINESS_ID = "2729063490586005"
BUSINESS_TOKEN = "EAAG-business-token-xyz"


@dataclass
class FakeMeta:
    app_id: str
    scopes: list[str] = field(
        default_factory=lambda: ["whatsapp_business_management", "whatsapp_business_messaging"]
    )
    token_valid: bool = True
    phone_numbers: list[dict[str, Any]] = field(default_factory=list)
    templates: list[dict[str, Any]] = field(default_factory=list)
    subscribed: bool = False
    registered: dict[str, str] = field(default_factory=dict)
    sent: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    # path regex -> (status, body) to force an error response
    failures: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    _counter: int = 0

    def __post_init__(self) -> None:
        if not self.phone_numbers:
            self.phone_numbers = [
                {
                    "id": PHONE_NUMBER_ID,
                    "display_phone_number": DISPLAY_PHONE,
                    "verified_name": "Sunrise Dental",
                    "quality_rating": "GREEN",
                    "code_verification_status": "VERIFIED",
                    "name_status": "APPROVED",
                    "platform_type": "NOT_APPLICABLE",
                    "status": "CONNECTED",
                    "messaging_limit_tier": "TIER_1K",
                }
            ]

    def fail(self, path_pattern: str, status: int, code: int, message: str = "error") -> None:
        self.failures[path_pattern] = (
            status,
            {"error": {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": "TRACE"}},
        )

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        assert path.startswith("/v25.0/"), path
        path = path[len("/v25.0") :]
        for pattern, (status, body) in self.failures.items():
            if re.fullmatch(pattern, f"{request.method} {path}"):
                return httpx.Response(status, json=body)
        q = request.url.params
        method = request.method

        if path == "/oauth/access_token":
            assert q["client_id"] == self.app_id and q["client_secret"]
            if q["code"] != "good-code":
                return httpx.Response(
                    400, json={"error": {"message": "Invalid verification code format.", "code": 100}}
                )
            return httpx.Response(200, json={"access_token": BUSINESS_TOKEN, "token_type": "bearer"})
        if path == "/debug_token":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "app_id": self.app_id,
                        "is_valid": self.token_valid,
                        "scopes": self.scopes,
                        "granular_scopes": [
                            {"scope": "whatsapp_business_management", "target_ids": [WABA_ID]},
                            {"scope": "whatsapp_business_messaging", "target_ids": [WABA_ID]},
                        ],
                        "expires_at": 0,
                    }
                },
            )
        self._require_token(request)
        if path == f"/{WABA_ID}" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": WABA_ID,
                    "name": "Sunrise Dental WABA",
                    "currency": "INR",
                    "timezone_id": "71",
                    "message_template_namespace": "ns_123",
                    "owner_business_info": {"id": BUSINESS_ID, "name": "Sunrise Dental"},
                },
            )
        if path == f"/{WABA_ID}/phone_numbers":
            return httpx.Response(200, json={"data": self.phone_numbers})
        if path == f"/{WABA_ID}/subscribed_apps":
            if method == "POST":
                self.subscribed = True
                return httpx.Response(200, json={"success": True})
            if method == "DELETE":
                self.subscribed = False
                return httpx.Response(200, json={"success": True})
            data = [{"whatsapp_business_api_data": {"id": self.app_id, "name": "XploWhatsApp"}}]
            return httpx.Response(200, json={"data": data if self.subscribed else []})
        if path == f"/{WABA_ID}/message_templates":
            after = q.get("after")
            page = self.templates[1:] if after == "c1" else self.templates[:1]
            paging: dict[str, Any] = {"cursors": {"before": "c0", "after": "c1"}}
            if after != "c1" and len(self.templates) > 1:
                paging["next"] = "https://graph.facebook.com/next"
            return httpx.Response(200, json={"data": page, "paging": paging})
        m = re.fullmatch(r"/(\d+)/register", path)
        if m and method == "POST":
            body = json.loads(request.content)
            assert body["messaging_product"] == "whatsapp" and re.fullmatch(r"\d{6}", body["pin"])
            self.registered[m.group(1)] = body["pin"]
            return httpx.Response(200, json={"success": True})
        m = re.fullmatch(r"/(\d+)/messages", path)
        if m and method == "POST":
            body = json.loads(request.content)
            self.sent.append(body)
            self._counter += 1
            wamid = f"wamid.OUT{self._counter:04d}"
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "contacts": [{"input": body.get("to"), "wa_id": body.get("to")}],
                    "messages": [{"id": wamid}],
                },
            )
        return httpx.Response(404, json={"error": {"message": f"unhandled {method} {path}", "code": 100}})

    def _require_token(self, request: httpx.Request) -> None:
        assert request.headers.get("authorization") == f"Bearer {BUSINESS_TOKEN}", "business token expected"

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# ---- webhook payload builders (shapes follow Meta's webhook reference) -----------


def envelope(field_name: str, value: dict[str, Any], entry_id: str = WABA_ID) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": entry_id, "changes": [{"field": field_name, "value": value}]}],
    }


def metadata(phone_number_id: str = PHONE_NUMBER_ID) -> dict[str, Any]:
    return {"display_phone_number": "919876543210", "phone_number_id": phone_number_id}


def inbound_text(
    text: str,
    *,
    wamid: str = "wamid.IN0001",
    wa_id: str | None = "919812345678",
    bsuid: str | None = "IN.1349120865530274",
    name: str = "Priya",
    ts: int | None = None,
    phone_number_id: str = PHONE_NUMBER_ID,
) -> dict[str, Any]:
    contact: dict[str, Any] = {"profile": {"name": name}}
    msg: dict[str, Any] = {
        "id": wamid,
        "timestamp": str(ts or int(time.time())),
        "type": "text",
        "text": {"body": text},
    }
    if wa_id:
        contact["wa_id"] = wa_id
        msg["from"] = wa_id
    if bsuid:
        contact["user_id"] = bsuid
        msg["from_user_id"] = bsuid
    return envelope(
        "messages",
        {
            "messaging_product": "whatsapp",
            "metadata": metadata(phone_number_id),
            "contacts": [contact],
            "messages": [msg],
        },
    )


def status_update(
    wamid: str, status: str, *, ts: int | None = None, errors: list[Any] | None = None
) -> dict[str, Any]:
    st: dict[str, Any] = {
        "id": wamid,
        "status": status,
        "timestamp": str(ts or int(time.time())),
        "recipient_id": "919812345678",
        "recipient_user_id": "IN.1349120865530274",
    }
    if status in {"sent", "delivered", "read"}:
        st["pricing"] = {"billable": True, "pricing_model": "PMP", "category": "service", "type": "regular"}
    if errors:
        st["errors"] = errors
    return envelope("messages", {"messaging_product": "whatsapp", "metadata": metadata(), "statuses": [st]})


def signed(payload: dict[str, Any], secret: str) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    return body, {"X-Hub-Signature-256": compute_signature(body, secret), "Content-Type": "application/json"}
