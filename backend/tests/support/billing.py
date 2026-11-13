"""Fake Razorpay and Stripe APIs for tests. They answer the endpoints XploWhatsApp uses
with responses shaped like the documented ones, keep the objects they create, record
every request, and build signed webhooks the way each gateway signs them."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

import httpx

RAZORPAY_KEY_SECRET = "rzp-test-secret"
RAZORPAY_WEBHOOK_SECRET = "rzp-webhook-secret"
STRIPE_WEBHOOK_SECRET = "whsec_test"
DAY = 86400


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:14]}"


def unflatten(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Stripe's bracket form back into nested data: items[0][price]=p -> {"items": {"0": {...}}}."""
    out: dict[str, Any] = {}
    for key, value in pairs:
        parts = key.replace("]", "").split("[")
        node = out
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


def _list(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [value[k] for k in sorted(value, key=int)]
    return list(value or [])


@dataclass
class FakeGateways:
    requests: list[tuple[str, str, str, Any]] = field(default_factory=list)
    # Razorpay
    rzp_plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    rzp_subs: dict[str, dict[str, Any]] = field(default_factory=dict)
    rzp_addons: list[dict[str, Any]] = field(default_factory=list)
    # Stripe
    prices: dict[str, dict[str, Any]] = field(default_factory=dict)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    subs: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail_next: tuple[int, dict[str, Any]] | None = None
    checkout_base: str = "https://checkout.stripe.com/c/pay"

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def calls(self, method: str, prefix: str) -> list[Any]:
        return [body for (_, m, path, body) in self.requests if m == method and path.startswith(prefix)]

    # ---- request handling -----------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, method, path = request.url.host, request.method, request.url.path
        if host == "api.razorpay.com":
            body = json.loads(request.content) if request.content else None
            self.requests.append(("razorpay", method, path, body))
            assert request.headers["authorization"].startswith("Basic ")
        else:
            assert host == "api.stripe.com", host
            assert request.headers["authorization"] == "Bearer sk_test_stripe"
            raw = request.content.decode() if method != "GET" else request.url.query.decode()
            body = unflatten(parse_qsl(raw))
            self.requests.append(("stripe", method, path, body))
        if self.fail_next is not None:
            status, payload = self.fail_next
            self.fail_next = None
            return httpx.Response(status, json=payload)
        if host == "api.razorpay.com":
            return self._razorpay(method, path, body or {})
        return self._stripe(method, path, body)

    def _razorpay(self, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
        parts = path.strip("/").split("/")[1:]  # drop v1
        if parts == ["plans"] and method == "POST":
            plan = {"id": _id("plan"), "entity": "plan", **body}
            self.rzp_plans[plan["id"]] = plan
            return httpx.Response(200, json=plan)
        if parts == ["subscriptions"] and method == "POST":
            assert body["plan_id"] in self.rzp_plans
            sub = {
                "id": _id("sub"),
                "entity": "subscription",
                "plan_id": body["plan_id"],
                "status": "created",
                "quantity": body.get("quantity", 1),
                "total_count": body["total_count"],
                "notes": body.get("notes") or {},
                "addons": body.get("addons") or [],
                "customer_id": None,
                "current_start": None,
                "current_end": None,
                "short_url": "https://rzp.io/i/test",
            }
            self.rzp_subs[sub["id"]] = sub
            return httpx.Response(200, json=sub)
        if parts[0] == "subscriptions" and len(parts) >= 2:
            sub = self.rzp_subs.get(parts[1])
            if sub is None:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "code": "BAD_REQUEST_ERROR",
                            "description": "The id provided does not exist",
                        }
                    },
                )
            action = parts[2] if len(parts) > 2 else None
            if action is None and method == "GET":
                return httpx.Response(200, json=sub)
            if action is None and method == "PATCH":
                if body.get("schedule_change_at") == "now":
                    sub["plan_id"] = body["plan_id"]
                else:
                    sub["has_scheduled_changes"] = True
                    sub["scheduled_plan_id"] = body["plan_id"]
                return httpx.Response(200, json=sub)
            if action == "cancel_scheduled_changes":
                sub["has_scheduled_changes"] = False
                sub.pop("scheduled_plan_id", None)
                return httpx.Response(200, json=sub)
            if action == "addons":
                addon = {"id": _id("ao"), "entity": "addon", "subscription_id": sub["id"], **body}
                self.rzp_addons.append(addon)
                return httpx.Response(200, json=addon)
            if action == "cancel":
                if not body.get("cancel_at_cycle_end"):
                    sub["status"] = "cancelled"
                return httpx.Response(200, json=sub)
        raise AssertionError(f"unexpected Razorpay call {method} {path}")

    def _stripe(self, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
        parts = path.strip("/").split("/")[1:]
        if parts == ["products"]:
            return httpx.Response(200, json={"id": _id("prod"), "object": "product", **body})
        if parts == ["prices"]:
            price = {"id": _id("price"), "object": "price", **body}
            self.prices[price["id"]] = price
            return httpx.Response(200, json=price)
        if parts == ["checkout", "sessions"] and method == "POST":
            assert body["mode"] == "subscription"
            assert "{CHECKOUT_SESSION_ID}" in body["success_url"]
            cs = {
                "id": _id("cs_test"),
                "object": "checkout.session",
                "mode": "subscription",
                "status": "open",
                "url": "",
                "success_url": body["success_url"],
                "metadata": body.get("metadata") or {},
                "line_items": _list(body.get("line_items")),
                "subscription": None,
                "customer": body.get("customer"),
            }
            cs["url"] = f"{self.checkout_base}/{cs['id']}"
            self.sessions[cs["id"]] = cs
            return httpx.Response(200, json=cs)
        if parts[:2] == ["checkout", "sessions"] and method == "GET":
            return httpx.Response(200, json=self.sessions[parts[2]])
        if parts[0] == "subscriptions":
            sub = self.subs[parts[1]]
            if method == "GET":
                return httpx.Response(200, json=sub)
            if method == "DELETE":
                sub["status"] = "canceled"
                return httpx.Response(200, json=sub)
            if "cancel_at_period_end" in body:
                sub["cancel_at_period_end"] = body["cancel_at_period_end"] == "true"
            for change in _list(body.get("items")):
                items = sub["items"]["data"]
                if "id" not in change:
                    items.append(self._item(change["price"], int(change.get("quantity", 1)), sub))
                    continue
                item = next(i for i in items if i["id"] == change["id"])
                if change.get("deleted") == "true":
                    items.remove(item)
                    continue
                if "price" in change:
                    item["price"] = {"id": change["price"]}
                if "quantity" in change:
                    item["quantity"] = int(change["quantity"])
            sub["last_proration_behavior"] = body.get("proration_behavior")
            return httpx.Response(200, json=sub)
        raise AssertionError(f"unexpected Stripe call {method} {path}")

    def _item(self, price_id: str, quantity: int, sub: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": _id("si"),
            "price": {"id": price_id},
            "quantity": quantity,
            "current_period_start": sub["start"],
            "current_period_end": sub["end"],
        }

    # ---- what the customer does on the gateway's page ---------------------------------------

    def razorpay_pay(self, sub_id: str, days: int = 30) -> dict[str, Any]:
        """The first payment succeeds and Razorpay activates the subscription."""
        sub = self.rzp_subs[sub_id]
        now = int(time.time())
        sub.update(
            status="active", customer_id="cust_test123", current_start=now, current_end=now + days * DAY
        )
        return sub

    def razorpay_checkout_signature(self, payment_id: str, sub_id: str) -> str:
        msg = f"{payment_id}|{sub_id}".encode()
        return hmac.new(RAZORPAY_KEY_SECRET.encode(), msg, hashlib.sha256).hexdigest()

    def stripe_pay(self, session_id: str, days: int = 30) -> dict[str, Any]:
        cs = self.sessions[session_id]
        now = int(time.time())
        sub: dict[str, Any] = {
            "id": _id("sub"),
            "object": "subscription",
            "status": "active",
            "customer": cs["customer"] or "cus_test123",
            "cancel_at_period_end": False,
            "metadata": cs["metadata"],
            "start": now,
            "end": now + days * DAY,
            "items": {"object": "list", "data": []},
        }
        for line in cs["line_items"]:
            sub["items"]["data"].append(self._item(line["price"], int(line.get("quantity", 1)), sub))
        self.subs[sub["id"]] = sub
        cs.update(status="complete", subscription=sub["id"], customer=sub["customer"])
        return sub

    def stripe_subscription_object(self, sub_id: str) -> dict[str, Any]:
        sub = dict(self.subs[sub_id])
        sub.pop("start"), sub.pop("end")
        return sub

    # ---- webhooks ------------------------------------------------------------------------------

    @staticmethod
    def razorpay_webhook(
        event: str, payload: dict[str, Any], event_id: str | None = None
    ) -> tuple[bytes, dict[str, str]]:
        body = json.dumps(
            {
                "entity": "event",
                "event": event,
                "contains": list(payload),
                "payload": payload,
                "created_at": int(time.time()),
            }
        ).encode()
        sig = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return body, {
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": event_id or _id("evt"),
            "Content-Type": "application/json",
        }

    def razorpay_charged(
        self, sub_id: str, amount_minor: int, payment_id: str | None = None
    ) -> tuple[bytes, dict[str, str]]:
        sub = self.rzp_subs[sub_id]
        payment = {
            "id": payment_id or _id("pay"),
            "entity": "payment",
            "amount": amount_minor,
            "currency": "INR",
            "status": "captured",
            "method": "upi",
            "subscription_id": sub_id,
            "notes": sub["notes"],
        }
        return self.razorpay_webhook(
            "subscription.charged",
            {"subscription": {"entity": sub}, "payment": {"entity": payment}},
        )

    @staticmethod
    def stripe_webhook(
        event_type: str, obj: dict[str, Any], *, secret: str = STRIPE_WEBHOOK_SECRET, ts: int | None = None
    ) -> tuple[bytes, dict[str, str]]:
        body = json.dumps(
            {
                "id": _id("evt"),
                "object": "event",
                "type": event_type,
                "data": {"object": obj},
                "created": int(time.time()),
            }
        ).encode()
        ts = ts or int(time.time())
        sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return body, {"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"}

    def stripe_invoice(self, sub_id: str, *, paid: bool = True, attempt: int = 1) -> dict[str, Any]:
        sub = self.subs[sub_id]
        lines = []
        total = 0
        for item in sub["items"]["data"]:
            unit = int(self.prices[item["price"]["id"]]["unit_amount"])
            amount = unit * int(item["quantity"])
            total += amount
            label = self.prices[item["price"]["id"]].get("nickname") or "Plan"
            lines.append(
                {
                    "description": f"{item['quantity']} x {label}",
                    "amount": amount,
                    "period": {"start": sub["start"], "end": sub["end"]},
                }
            )
        return {
            "id": _id("in"),
            "object": "invoice",
            "currency": self.prices[sub["items"]["data"][0]["price"]["id"]]["currency"],
            "customer": sub["customer"],
            "amount_due": total,
            "amount_paid": total if paid else 0,
            "attempt_count": attempt,
            "parent": {
                "type": "subscription_details",
                "subscription_details": {"subscription": sub_id, "metadata": sub["metadata"]},
            },
            "lines": {"object": "list", "data": lines},
        }
