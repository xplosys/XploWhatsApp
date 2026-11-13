"""The payment gateway interface. Subscription logic talks to this, never to Razorpay
or Stripe directly (ADR-012)."""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
from fastapi import status

from app.core.errors import AppError
from app.modules.billing.models import PlanPrice, Subscription, SubscriptionPlan

# Currencies with two decimal places that the gateways and invoices support.
CURRENCIES = ("INR", "USD", "GBP", "EUR")

_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport_override(transport: httpx.AsyncBaseTransport | None) -> None:
    """Tests route gateway calls to fakes built from the documented API shapes."""
    global _transport_override
    _transport_override = transport


def transport() -> httpx.AsyncBaseTransport | None:
    return _transport_override


class GatewayNotConfigured(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "gateway_not_configured"
    message = "Payments are not set up yet. Please contact support."


class GatewayError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "gateway_error"
    message = "The payment provider could not complete the request. Please try again."


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def with_tax(amount: Decimal, rate: Decimal) -> Decimal:
    return money(amount * (1 + rate / 100))


def minor_units(amount: Decimal) -> int:
    return int(money(amount) * 100)


def response_json(r: httpx.Response) -> dict[str, Any]:
    """The gateway's JSON body, or {} when it sent none or something else (a proxy's
    error page, say): the caller then reports a gateway error instead of crashing."""
    if not r.content:
        return {}
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def from_minor(value: int | str | None) -> Decimal:
    return money(Decimal(int(value or 0)) / 100)


@dataclass
class CheckoutStart:
    ref: str
    # What the browser needs: Razorpay Checkout options, or a Stripe Checkout URL.
    client: dict[str, Any]


@dataclass
class CheckoutRequest:
    tenant_id: uuid.UUID
    price: PlanPrice
    plan: SubscriptionPlan
    extra_seats: int
    email: str | None
    name: str | None
    phone: str | None
    customer_id: str | None
    success_url: str
    cancel_url: str


@dataclass
class GatewaySubscription:
    """A subscription as the gateway reports it, in our terms."""

    ref: str
    status: str  # active, past_due, canceled, pending (not paid yet)
    customer_id: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    cancel_at_period_end: bool = False
    gateway_plan_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentGateway(abc.ABC):
    name: str

    @abc.abstractmethod
    def configured(self) -> bool: ...

    @abc.abstractmethod
    async def create_price(self, plan: SubscriptionPlan, price: PlanPrice) -> tuple[str, str | None]:
        """Create the gateway's plan or price; returns (plan or price ID, seat price ID)."""

    @abc.abstractmethod
    async def start_checkout(self, req: CheckoutRequest) -> CheckoutStart: ...

    @abc.abstractmethod
    async def change_price(
        self, sub: Subscription, old: PlanPrice, new: PlanPrice, *, immediate: bool
    ) -> None: ...

    @abc.abstractmethod
    async def set_seats(
        self, sub: Subscription, price: PlanPrice, old: int, new: int, *, period_fraction_left: Decimal
    ) -> None: ...

    @abc.abstractmethod
    async def cancel(self, sub: Subscription, *, at_period_end: bool) -> None: ...

    @abc.abstractmethod
    async def resume(self, sub: Subscription) -> None: ...

    @abc.abstractmethod
    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool: ...


def gateway_for_currency(currency: str) -> str:
    from app.modules.billing.models import Gateway

    return Gateway.RAZORPAY if currency == "INR" else Gateway.STRIPE


def get_gateway(name: str) -> PaymentGateway:
    from app.modules.billing.gateways.razorpay import Razorpay
    from app.modules.billing.gateways.stripe import Stripe

    if name == "razorpay":
        return Razorpay()
    if name == "stripe":
        return Stripe()
    raise ValueError(f"unknown gateway {name}")
