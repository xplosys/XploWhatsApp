"""Stripe Billing for currencies other than INR (https://docs.stripe.com/api).

Requests are form-encoded with bracket notation for nested fields. Extra seats are a
second subscription item with its own per-seat price, so Stripe prorates them."""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog

from app.core.config import get_settings
from app.modules.billing.gateways.base import (
    CheckoutRequest,
    CheckoutStart,
    GatewayError,
    GatewayNotConfigured,
    GatewaySubscription,
    PaymentGateway,
    minor_units,
    transport,
    with_tax,
)
from app.modules.billing.models import PlanPrice, Subscription, SubscriptionPlan

log = structlog.get_logger(__name__)

STATUS = {
    "active": "active",
    "trialing": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
    "paused": "past_due",
    "canceled": "canceled",
    "incomplete_expired": "canceled",
    "incomplete": "pending",
}


def form(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """{"items": [{"price": "p"}]} -> [("items[0][price]", "p")]"""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            out += form(value, name)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out += form(item, f"{name}[{i}]")
                else:
                    out.append((f"{name}[{i}]", str(item)))
        elif isinstance(value, bool):
            out.append((name, "true" if value else "false"))
        elif value is not None:
            out.append((name, str(value)))
    return out


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


def parse_subscription(obj: dict[str, Any]) -> GatewaySubscription:
    items = (obj.get("items") or {}).get("data") or []
    first = items[0] if items else {}
    # Newer API versions report the period on each item instead of the subscription.
    start = obj.get("current_period_start") or first.get("current_period_start")
    end = obj.get("current_period_end") or first.get("current_period_end")
    return GatewaySubscription(
        ref=str(obj["id"]),
        status=STATUS.get(str(obj.get("status")), "pending"),
        customer_id=obj.get("customer") if isinstance(obj.get("customer"), str) else None,
        period_start=_ts(start),
        period_end=_ts(end),
        cancel_at_period_end=bool(obj.get("cancel_at_period_end")),
        gateway_plan_id=((first.get("price") or {}).get("id")) if first else None,
        raw=obj,
    )


class Stripe(PaymentGateway):
    name = "stripe"

    def __init__(self) -> None:
        s = get_settings()
        self._secret = s.stripe_secret_key.get_secret_value() if s.stripe_secret_key else None
        self._webhook_secret = s.stripe_webhook_secret.get_secret_value() if s.stripe_webhook_secret else None
        self._base = s.stripe_base_url.rstrip("/")
        self._version = s.stripe_api_version
        self._timeout = s.billing_http_timeout_seconds
        self._tolerance = s.stripe_webhook_tolerance_seconds

    def configured(self) -> bool:
        return bool(self._secret)

    async def _call(self, method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._secret:
            raise GatewayNotConfigured()
        headers = {"Authorization": f"Bearer {self._secret}"}
        if self._version:
            headers["Stripe-Version"] = self._version
        if method == "POST":
            headers["Idempotency-Key"] = str(uuid.uuid4())
        async with httpx.AsyncClient(
            base_url=self._base, timeout=httpx.Timeout(self._timeout, connect=5.0), transport=transport()
        ) as client:
            try:
                if method == "GET":
                    r = await client.get(f"/v1{path}", params=dict(form(data or {})), headers=headers)
                else:
                    r = await client.request(
                        method, f"/v1{path}", data=dict(form(data or {})), headers=headers
                    )
            except httpx.HTTPError as exc:
                log.warning("stripe_unreachable", path=path, error=str(exc))
                raise GatewayError() from exc
        body: dict[str, Any] = r.json() if r.content else {}
        if r.status_code >= 400:
            err = body.get("error") or {}
            log.warning("stripe_error", path=path, status=r.status_code, code=err.get("code"))
            raise GatewayError(f"Stripe: {err.get('message') or 'the request failed'}", code="gateway_error")
        return body

    async def create_price(self, plan: SubscriptionPlan, price: PlanPrice) -> tuple[str, str | None]:
        product = await self._call(
            "POST", "/products", {"name": f"XploWhatsApp {plan.name}", "metadata": {"plan_code": plan.code}}
        )
        recurring = {"interval": price.interval}
        base = await self._call(
            "POST",
            "/prices",
            {
                "product": product["id"],
                "currency": price.currency.lower(),
                "unit_amount": minor_units(with_tax(price.amount, price.tax_rate)),
                "recurring": recurring,
                "metadata": {"price_id": str(price.id)},
            },
        )
        seat_id = None
        if price.seat_amount:
            seat = await self._call(
                "POST",
                "/prices",
                {
                    "product": product["id"],
                    "currency": price.currency.lower(),
                    "unit_amount": minor_units(with_tax(price.seat_amount, price.tax_rate)),
                    "recurring": recurring,
                    "nickname": "Extra seat",
                    "metadata": {"price_id": str(price.id), "kind": "seat"},
                },
            )
            seat_id = str(seat["id"])
        return str(base["id"]), seat_id

    async def start_checkout(self, req: CheckoutRequest) -> CheckoutStart:
        if not req.price.gateway_plan_id:
            raise GatewayNotConfigured("This price has no Stripe price yet.")
        items: list[dict[str, Any]] = [{"price": req.price.gateway_plan_id, "quantity": 1}]
        if req.extra_seats and req.price.gateway_seat_price_id:
            items.append({"price": req.price.gateway_seat_price_id, "quantity": req.extra_seats})
        meta = {"tenant_id": str(req.tenant_id), "price_id": str(req.price.id)}
        body: dict[str, Any] = {
            "mode": "subscription",
            "line_items": items,
            "success_url": f"{req.success_url}&session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": req.cancel_url,
            "client_reference_id": str(req.tenant_id),
            "metadata": meta,
            "subscription_data": {"metadata": meta},
        }
        if req.customer_id:
            body["customer"] = req.customer_id
        elif req.email:
            body["customer_email"] = req.email
        session = await self._call("POST", "/checkout/sessions", body)
        return CheckoutStart(ref=str(session["id"]), client={"url": session["url"]})

    async def fetch(self, ref: str) -> GatewaySubscription:
        return parse_subscription(await self._call("GET", f"/subscriptions/{ref}"))

    async def _items(self, sub: Subscription) -> list[dict[str, Any]]:
        assert sub.gateway_subscription_id
        obj = await self._call("GET", f"/subscriptions/{sub.gateway_subscription_id}")
        return list((obj.get("items") or {}).get("data") or [])

    async def change_price(
        self, sub: Subscription, old: PlanPrice, new: PlanPrice, *, immediate: bool
    ) -> None:
        items = await self._items(sub)
        changes: list[dict[str, Any]] = []
        for item in items:
            price_id = (item.get("price") or {}).get("id")
            if price_id == old.gateway_plan_id:
                changes.append({"id": item["id"], "price": new.gateway_plan_id})
            elif price_id == old.gateway_seat_price_id:
                if new.gateway_seat_price_id:
                    changes.append({"id": item["id"], "price": new.gateway_seat_price_id})
                else:
                    changes.append({"id": item["id"], "deleted": True})
        await self._call(
            "POST",
            f"/subscriptions/{sub.gateway_subscription_id}",
            {"items": changes, "proration_behavior": "always_invoice" if immediate else "none"},
        )

    async def set_seats(
        self, sub: Subscription, price: PlanPrice, old: int, new: int, *, period_fraction_left: Decimal
    ) -> None:
        if not price.gateway_seat_price_id:
            return
        items = await self._items(sub)
        seat = next(
            (i for i in items if (i.get("price") or {}).get("id") == price.gateway_seat_price_id), None
        )
        if seat is None and new == 0:
            return
        if seat is None:
            change: dict[str, Any] = {"price": price.gateway_seat_price_id, "quantity": new}
        elif new == 0:
            change = {"id": seat["id"], "deleted": True}
        else:
            change = {"id": seat["id"], "quantity": new}
        await self._call(
            "POST",
            f"/subscriptions/{sub.gateway_subscription_id}",
            {"items": [change], "proration_behavior": "always_invoice" if new > old else "none"},
        )

    async def cancel(self, sub: Subscription, *, at_period_end: bool) -> None:
        assert sub.gateway_subscription_id
        if at_period_end:
            await self._call(
                "POST", f"/subscriptions/{sub.gateway_subscription_id}", {"cancel_at_period_end": True}
            )
        else:
            await self._call("DELETE", f"/subscriptions/{sub.gateway_subscription_id}")

    async def resume(self, sub: Subscription) -> None:
        assert sub.gateway_subscription_id
        await self._call(
            "POST", f"/subscriptions/{sub.gateway_subscription_id}", {"cancel_at_period_end": False}
        )

    async def checkout_session(self, session_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/checkout/sessions/{session_id}")

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool:
        header = headers.get("stripe-signature")
        if not self._webhook_secret or not header:
            return False
        parts: dict[str, list[str]] = {}
        for piece in header.split(","):
            k, _, v = piece.strip().partition("=")
            parts.setdefault(k, []).append(v)
        try:
            ts = int(parts["t"][0])
        except (KeyError, ValueError):
            return False
        if abs(time.time() - ts) > self._tolerance:
            return False
        signed = f"{ts}.".encode() + body
        expected = hmac.new(self._webhook_secret.encode(), signed, hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", []))
