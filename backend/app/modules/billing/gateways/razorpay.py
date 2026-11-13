"""Razorpay Subscriptions for INR (https://razorpay.com/docs/api/payments/subscriptions/).

Plans carry the GST-inclusive amount. Extra seats are add-ons: Razorpay charges an
add-on once, on the subscription's next invoice, so a seat added mid-period is billed
pro rata on the next invoice and each renewal adds one add-on for the coming period."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog

from app.core.config import get_settings
from app.core.errors import Conflict
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
    "created": "pending",
    "authenticated": "pending",
    "active": "active",
    "pending": "past_due",
    "halted": "past_due",
    "cancelled": "canceled",
    "completed": "canceled",
    "expired": "canceled",
    "paused": "past_due",
}


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


def parse_subscription(entity: dict[str, Any]) -> GatewaySubscription:
    return GatewaySubscription(
        ref=str(entity["id"]),
        status=STATUS.get(str(entity.get("status")), "pending"),
        customer_id=entity.get("customer_id"),
        period_start=_ts(entity.get("current_start")),
        period_end=_ts(entity.get("current_end")),
        gateway_plan_id=entity.get("plan_id"),
        raw=entity,
    )


class Razorpay(PaymentGateway):
    name = "razorpay"

    def __init__(self) -> None:
        s = get_settings()
        self.key_id = s.razorpay_key_id
        self._secret = s.razorpay_key_secret.get_secret_value() if s.razorpay_key_secret else None
        self._webhook_secret = (
            s.razorpay_webhook_secret.get_secret_value() if s.razorpay_webhook_secret else None
        )
        self._base = s.razorpay_base_url.rstrip("/")
        self._timeout = s.billing_http_timeout_seconds
        self._cycles = {"month": s.razorpay_monthly_cycles, "year": s.razorpay_yearly_cycles}

    def configured(self) -> bool:
        return bool(self.key_id and self._secret)

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured():
            raise GatewayNotConfigured()
        assert self.key_id and self._secret
        async with httpx.AsyncClient(
            base_url=self._base,
            auth=(self.key_id, self._secret),
            timeout=httpx.Timeout(self._timeout, connect=5.0),
            transport=transport(),
        ) as client:
            try:
                r = await client.request(method, f"/v1{path}", json=body)
            except httpx.HTTPError as exc:
                log.warning("razorpay_unreachable", path=path, error=str(exc))
                raise GatewayError() from exc
        data: dict[str, Any] = r.json() if r.content else {}
        if r.status_code >= 400:
            err = data.get("error") or {}
            log.warning("razorpay_error", path=path, status=r.status_code, code=err.get("code"))
            raise GatewayError(
                f"Razorpay: {err.get('description') or 'the request failed'}", code="gateway_error"
            )
        return data

    async def create_price(self, plan: SubscriptionPlan, price: PlanPrice) -> tuple[str, str | None]:
        body = {
            "period": "monthly" if price.interval == "month" else "yearly",
            "interval": 1,
            "item": {
                "name": f"XploWhatsApp {plan.name}",
                "amount": minor_units(with_tax(price.amount, price.tax_rate)),
                "currency": price.currency,
                "description": f"{plan.name}, billed {'monthly' if price.interval == 'month' else 'yearly'}",
            },
            "notes": {"plan_code": plan.code, "price_id": str(price.id)},
        }
        return str((await self._call("POST", "/plans", body))["id"]), None

    def _seat_item(self, price: PlanPrice, seats: int, fraction: Decimal, label: str) -> dict[str, Any]:
        assert price.seat_amount is not None
        return {
            "name": label,
            "amount": minor_units(with_tax(price.seat_amount * seats * fraction, price.tax_rate)),
            "currency": price.currency,
        }

    async def start_checkout(self, req: CheckoutRequest) -> CheckoutStart:
        if not req.price.gateway_plan_id:
            raise GatewayNotConfigured("This price has no Razorpay plan yet.")
        body: dict[str, Any] = {
            "plan_id": req.price.gateway_plan_id,
            "total_count": self._cycles[req.price.interval],
            "quantity": 1,
            "customer_notify": 1,
            "notes": {"tenant_id": str(req.tenant_id), "price_id": str(req.price.id)},
        }
        if req.extra_seats and req.price.seat_amount:
            body["addons"] = [
                {
                    "item": self._seat_item(
                        req.price, req.extra_seats, Decimal(1), f"{req.extra_seats} extra seats"
                    )
                }
            ]
        sub = await self._call("POST", "/subscriptions", body)
        prefill = {k: v for k, v in {"name": req.name, "email": req.email, "contact": req.phone}.items() if v}
        return CheckoutStart(
            ref=str(sub["id"]),
            client={
                "key": self.key_id,
                "subscription_id": sub["id"],
                "name": "XploWhatsApp",
                "description": req.plan.name,
                "prefill": prefill,
                "notes": {"tenant_id": str(req.tenant_id)},
            },
        )

    async def fetch(self, ref: str) -> GatewaySubscription:
        return parse_subscription(await self._call("GET", f"/subscriptions/{ref}"))

    async def change_price(
        self, sub: Subscription, old: PlanPrice, new: PlanPrice, *, immediate: bool
    ) -> None:
        assert sub.gateway_subscription_id
        await self._call(
            "PATCH",
            f"/subscriptions/{sub.gateway_subscription_id}",
            {
                "plan_id": new.gateway_plan_id,
                "quantity": 1,
                "schedule_change_at": "now" if immediate else "cycle_end",
                "customer_notify": 1,
            },
        )

    async def cancel_scheduled_change(self, sub: Subscription) -> None:
        assert sub.gateway_subscription_id
        await self._call("POST", f"/subscriptions/{sub.gateway_subscription_id}/cancel_scheduled_changes", {})

    async def set_seats(
        self, sub: Subscription, price: PlanPrice, old: int, new: int, *, period_fraction_left: Decimal
    ) -> None:
        if new <= old or not price.seat_amount:
            return  # fewer seats: the next renewal's add-on uses the new number
        added = new - old
        await self.add_seat_addon(
            sub, price, added, period_fraction_left, f"{added} extra seats, rest of this period"
        )

    async def add_seat_addon(
        self, sub: Subscription, price: PlanPrice, seats: int, fraction: Decimal, label: str
    ) -> None:
        assert sub.gateway_subscription_id
        if seats <= 0 or not price.seat_amount:
            return
        await self._call(
            "POST",
            f"/subscriptions/{sub.gateway_subscription_id}/addons",
            {"item": self._seat_item(price, seats, fraction, label), "quantity": 1},
        )

    async def cancel(self, sub: Subscription, *, at_period_end: bool) -> None:
        assert sub.gateway_subscription_id
        await self._call(
            "POST",
            f"/subscriptions/{sub.gateway_subscription_id}/cancel",
            {"cancel_at_cycle_end": 1 if at_period_end else 0},
        )

    async def resume(self, sub: Subscription) -> None:
        raise Conflict(
            "A cancellation through Razorpay can't be undone. Choose a plan again after it ends.",
            code="cannot_resume",
        )

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool:
        signature = headers.get("x-razorpay-signature")
        if not self._webhook_secret or not signature:
            return False
        expected = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def verify_checkout(self, payment_id: str, subscription_id: str, signature: str) -> bool:
        """The signature Checkout returns after the first payment of a subscription."""
        if not self._secret:
            return False
        msg = f"{payment_id}|{subscription_id}".encode()
        expected = hmac.new(self._secret.encode(), msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
