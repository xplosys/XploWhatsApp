"""Billing: plans and prices, checkout through Razorpay and Stripe, gateway webhooks,
tax invoices, plan and seat changes, cancellation, failed payments and trials."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.db.session import admin_transaction
from app.modules.billing.models import PlanPrice, SubscriptionPlan
from tests.conftest import Actor, RegisterFn
from tests.integration.test_ai import super_admin
from tests.support.billing import FakeGateways

API = "/api/v1"
INDIA_GSTIN = "27AAACB5678C1ZR"  # Maharashtra; the seller in tests is in Karnataka (29)


@pytest.fixture
def gw() -> Iterator[FakeGateways]:
    from app.modules.billing.gateways.base import set_transport_override

    fake = FakeGateways()
    set_transport_override(fake.transport)
    yield fake
    set_transport_override(None)


async def _price(
    session: Any, plan: SubscriptionPlan, currency: str, interval: str, amount: str, seat: str | None = None
) -> PlanPrice:
    from app.modules.billing.gateways.base import gateway_for_currency, get_gateway
    from app.modules.billing.service import tax_rate_for

    name = gateway_for_currency(currency)
    price = PlanPrice(
        id=uuid.uuid4(),
        plan_id=plan.id,
        currency=currency,
        interval=interval,
        amount=Decimal(amount),
        seat_amount=Decimal(seat) if seat else None,
        tax_rate=tax_rate_for(currency),
        gateway=name,
    )
    price.gateway_plan_id, price.gateway_seat_price_id = await get_gateway(name).create_price(plan, price)
    session.add(price)
    return price


@pytest.fixture
async def catalogue(gw: FakeGateways, database: Any) -> AsyncIterator[dict[str, uuid.UUID]]:
    """Starter and Growth plans, priced in INR (Razorpay) and USD (Stripe)."""
    tag = uuid.uuid4().hex[:6]
    ids: dict[str, uuid.UUID] = {}
    async with admin_transaction() as s:
        starter = SubscriptionPlan(
            code=f"starter-{tag}",
            name="Starter",
            limits={
                "max_users": 2,
                "max_whatsapp_numbers": 1,
                "max_knowledge_documents": 10,
                "max_storage_mb": 100,
                "monthly_ai_messages": 1000,
                "platform_ai_keys": 1,
            },
            features=["1 WhatsApp number"],
            sort_order=1,
        )
        growth = SubscriptionPlan(
            code=f"growth-{tag}",
            name="Growth",
            limits={
                "max_users": 5,
                "max_whatsapp_numbers": 2,
                "max_knowledge_documents": 100,
                "max_storage_mb": 1000,
                "monthly_ai_messages": 10000,
                "platform_ai_keys": 1,
            },
            features=["2 WhatsApp numbers", "Extra seats"],
            sort_order=2,
        )
        s.add_all([starter, growth])
        await s.flush()
        for key, args in {
            "starter_inr": (starter, "INR", "month", "1000.00"),
            "growth_inr": (growth, "INR", "month", "2000.00", "300.00"),
            "growth_inr_year": (growth, "INR", "year", "20000.00", "3000.00"),
            "starter_usd": (starter, "USD", "month", "15.00"),
            "growth_usd": (growth, "USD", "month", "30.00", "5.00"),
        }.items():
            ids[key] = (await _price(s, *args)).id  # type: ignore[arg-type]
    yield ids
    async with admin_transaction() as s:  # plans outlive the per-test truncation; hide these
        for plan in (starter, growth):
            (await s.get(SubscriptionPlan, plan.id)).is_active = False  # type: ignore[union-attr]


async def drain(*, expect: str | None = None) -> list[str]:
    from app.modules.billing.events import process
    from app.workers.dispatch import BILLING_EVENT, DISPATCHED

    outcomes = []
    for job in [j for j in DISPATCHED if j.task == BILLING_EVENT]:
        DISPATCHED.remove(job)
        outcomes.append(await process(uuid.UUID(job.args[0]), uuid.UUID(job.args[1])))
    if expect is not None:
        assert outcomes == [expect], outcomes
    return outcomes


async def send(
    client: httpx.AsyncClient, gateway: str, signed: tuple[bytes, dict[str, str]]
) -> httpx.Response:
    body, headers = signed
    return await client.post(f"{API}/webhooks/{gateway}", content=body, headers=headers)


async def india_owner(register: RegisterFn, email: str = "owner@acme.in", gstin: str = INDIA_GSTIN) -> Actor:
    owner = await register(email, "Acme Clinic")
    r = await owner.put(
        f"{API}/billing/profile",
        json={
            "legal_name": "Acme Clinic Pvt Ltd",
            "address_line1": "4 Linking Road",
            "city": "Mumbai",
            "postal_code": "400050",
            "gstin": gstin,
            "email": "accounts@acme.in",
        },
    )
    assert r.status_code == 200, r.text
    return owner


async def razorpay_subscribe(owner: Actor, gw: FakeGateways, price_id: uuid.UUID, seats: int = 0) -> str:
    r = await owner.post(f"{API}/billing/checkout", json={"price_id": str(price_id), "extra_seats": seats})
    assert r.status_code == 201, r.text
    sub_id = r.json()["razorpay"]["subscription_id"]
    gw.razorpay_pay(sub_id)
    r = await owner.post(
        f"{API}/billing/checkout/razorpay/verify",
        json={
            "razorpay_payment_id": "pay_first",
            "razorpay_subscription_id": sub_id,
            "razorpay_signature": gw.razorpay_checkout_signature("pay_first", sub_id),
        },
    )
    assert r.status_code == 200 and r.json()["status"] == "active", r.text
    return sub_id


async def stripe_subscribe(
    owner: Actor, gw: FakeGateways, client: httpx.AsyncClient, price_id: uuid.UUID, seats: int = 0
) -> str:
    r = await owner.post(f"{API}/billing/checkout", json={"price_id": str(price_id), "extra_seats": seats})
    assert r.status_code == 201, r.text
    cs_id = next(iter(gw.sessions))
    sub = gw.stripe_pay(cs_id)
    r = await send(client, "stripe", gw.stripe_webhook("checkout.session.completed", gw.sessions[cs_id]))
    assert r.status_code == 200
    await drain(expect="activated")
    return str(sub["id"])


# ---- catalogue -------------------------------------------------------------------------------


async def test_super_admin_manages_plans_and_prices(
    register: RegisterFn, client: httpx.AsyncClient, gw: FakeGateways
) -> None:
    owner = await register("owner@acme.in")
    assert (await owner.get(f"{API}/admin/billing/plans")).status_code == 403
    h = await super_admin(client)
    code = f"pro-{uuid.uuid4().hex[:6]}"

    r = await client.post(
        f"{API}/admin/billing/plans",
        headers=h,
        json={"code": code, "name": "Pro", "limits": {"max_seats": 3}},
    )
    assert r.status_code == 422
    r = await client.post(
        f"{API}/admin/billing/plans",
        headers=h,
        json={
            "code": code,
            "name": "Pro",
            "limits": {"max_users": 10, "monthly_ai_messages": 5000},
            "features": ["Priority support", " "],
        },
    )
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["features"] == ["Priority support"] and plan["prices"] == []

    # INR prices carry 18% GST (the seller has a GSTIN) and become a Razorpay plan.
    r = await client.post(
        f"{API}/admin/billing/plans/{plan['id']}/prices",
        headers=h,
        json={"currency": "inr", "interval": "month", "amount": "4999.00"},
    )
    assert r.status_code == 201, r.text
    inr = r.json()["prices"][0]
    assert inr["tax_rate"] == "18.00" and inr["amount_with_tax"] == "5898.82" and inr["gateway"] == "razorpay"
    assert next(iter(gw.rzp_plans.values()))["item"]["amount"] == 589882

    # USD is an export of services: no tax, a Stripe product with a base and a seat price.
    r = await client.post(
        f"{API}/admin/billing/plans/{plan['id']}/prices",
        headers=h,
        json={"currency": "USD", "interval": "year", "amount": "600", "seat_amount": "60"},
    )
    usd = next(p for p in r.json()["prices"] if p["currency"] == "USD")
    assert usd["tax_rate"] == "0.00" and usd["gateway_seat_price_id"]
    assert {p["unit_amount"] for p in gw.prices.values()} == {"60000", "6000"}

    # A new price for the same currency and interval retires the old one.
    r = await client.post(
        f"{API}/admin/billing/plans/{plan['id']}/prices",
        headers=h,
        json={"currency": "INR", "interval": "month", "amount": "5499"},
    )
    prices = r.json()["prices"]
    assert [(p["amount"], p["is_active"]) for p in prices if p["currency"] == "INR"] == [
        ("5499.00", True),
        ("4999.00", False),
    ]

    # The Indian business sees the INR price; a US business sees the USD one.
    listed = (await owner.get(f"{API}/billing/plans")).json()
    mine = next(p for p in listed["plans"] if p["code"] == code)
    assert listed["currencies"] == ["INR"] and [p["amount"] for p in mine["prices"]] == ["5499.00"]
    assert listed["payments_ready"] is True
    us = await register("o@acme.com", "Acme Inc", country="US")
    mine = next(p for p in (await us.get(f"{API}/billing/plans")).json()["plans"] if p["code"] == code)
    assert [p["currency"] for p in mine["prices"]] == ["USD"]

    r = await client.patch(
        f"{API}/admin/billing/plans/{plan['id']}",
        headers=h,
        json={"limits": {"max_users": 12}, "is_public": False},
    )
    assert r.json()["limits"] == {"max_users": 12, "monthly_ai_messages": 5000}
    assert all(p["code"] != code for p in (await owner.get(f"{API}/billing/plans")).json()["plans"])

    r = await client.delete(f"{API}/admin/billing/plans/{plan['id']}/prices/{usd['id']}", headers=h)
    assert not next(p for p in r.json()["prices"] if p["id"] == usd["id"])["is_active"]

    trial = next(
        p for p in (await client.get(f"{API}/admin/billing/plans", headers=h)).json() if p["code"] == "trial"
    )
    r = await client.post(
        f"{API}/admin/billing/plans/{trial['id']}/prices",
        headers=h,
        json={"currency": "INR", "interval": "month", "amount": "1"},
    )
    assert r.json()["error"]["code"] == "trial_has_no_price"
    r = await client.patch(f"{API}/admin/billing/plans/{trial['id']}", headers=h, json={"is_active": False})
    assert r.status_code == 422

    # A gateway failure stores nothing.
    gw.fail_next = (400, {"error": {"code": "BAD_REQUEST_ERROR", "description": "amount too small"}})
    r = await client.post(
        f"{API}/admin/billing/plans/{plan['id']}/prices",
        headers=h,
        json={"currency": "INR", "interval": "year", "amount": "1"},
    )
    assert r.status_code == 502 and "amount too small" in r.json()["error"]["message"]
    await client.patch(f"{API}/admin/billing/plans/{plan['id']}", headers=h, json={"is_active": False})


# ---- Razorpay --------------------------------------------------------------------------------


async def test_razorpay_checkout_invoices_and_seats(
    register: RegisterFn, client: httpx.AsyncClient, gw: FakeGateways, catalogue: dict[str, uuid.UUID]
) -> None:
    owner = await register("owner@acme.in", "Acme Clinic")
    body = {"price_id": str(catalogue["growth_inr"]), "extra_seats": 2}
    r = await owner.post(f"{API}/billing/checkout", json=body)
    assert r.json()["error"]["code"] == "billing_details_missing"
    r = await owner.put(f"{API}/billing/profile", json={"legal_name": "Acme", "gstin": "27AAACB5678C1ZX"})
    assert r.json()["error"]["code"] == "invalid_gstin"
    r = await owner.put(
        f"{API}/billing/profile", json={"legal_name": "Acme", "gstin": INDIA_GSTIN, "state_code": "29"}
    )
    assert r.json()["error"]["code"] == "gstin_state_mismatch"
    r = await owner.put(f"{API}/billing/profile", json={"legal_name": "Acme"})
    assert (await owner.post(f"{API}/billing/checkout", json=body)).json()["error"][
        "code"
    ] == "billing_state_missing"
    owner = await india_owner(register, "second@acme.in")
    r = await owner.post(f"{API}/billing/checkout", json={"price_id": str(catalogue["growth_usd"])})
    assert r.json()["error"]["code"] == "currency_not_offered"
    r = await owner.post(
        f"{API}/billing/checkout", json={"price_id": str(catalogue["starter_inr"]), "extra_seats": 1}
    )
    assert r.json()["error"]["code"] == "seats_not_offered"

    r = await owner.post(f"{API}/billing/checkout", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["gateway"] == "razorpay" and out["razorpay"]["key"] == "rzp_test_key"
    sub_id = out["razorpay"]["subscription_id"]
    created = gw.rzp_subs[sub_id]
    assert created["total_count"] == 120 and created["notes"]["tenant_id"] == owner.tenant_id
    assert created["addons"][0]["item"]["amount"] == 70800  # 2 seats x ₹300 + 18% GST

    # A forged success callback is refused; nothing changes until the payment is real.
    r = await owner.post(
        f"{API}/billing/checkout/razorpay/verify",
        json={
            "razorpay_payment_id": "pay_1",
            "razorpay_subscription_id": sub_id,
            "razorpay_signature": "0" * 64,
        },
    )
    assert r.status_code == 401
    assert (await owner.get(f"{API}/billing/subscription")).json()["status"] == "trialing"

    gw.razorpay_pay(sub_id)
    r = await owner.post(
        f"{API}/billing/checkout/razorpay/verify",
        json={
            "razorpay_payment_id": "pay_1",
            "razorpay_subscription_id": sub_id,
            "razorpay_signature": gw.razorpay_checkout_signature("pay_1", sub_id),
        },
    )
    sub = r.json()
    assert sub["status"] == "active" and sub["plan_name"] == "Growth" and sub["gateway"] == "razorpay"
    assert (
        sub["extra_seats"] == 2
        and sub["seats_included"] == 7
        and sub["price"]["amount_with_tax"] == "2360.00"
    )
    assert (await owner.post(f"{API}/billing/checkout", json=body)).json()["error"][
        "code"
    ] == "already_subscribed"

    # The charge webhook issues a GST invoice: inter-state, so IGST.
    charged = gw.razorpay_charged(sub_id, 306800, payment_id="pay_1")
    assert (await send(client, "razorpay", charged)).status_code == 200
    await drain(expect="charged")
    assert (await send(client, "razorpay", charged)).status_code == 200  # redelivery
    assert await drain() == []

    invoices = (await owner.get(f"{API}/billing/invoices")).json()
    assert len(invoices) == 1
    inv = (await owner.get(f"{API}/billing/invoices/{invoices[0]['id']}")).json()
    assert inv["number"].startswith("XW/") and inv["number"].endswith("/000001")
    assert (inv["subtotal"], inv["tax_total"], inv["total"]) == ("2600.00", "468.00", "3068.00")
    assert inv["taxes"] == [{"name": "IGST", "rate": "18", "amount": "468.00"}]
    assert [(i["description"], i["amount"], i["sac"]) for i in inv["line_items"]] == [
        ("XploWhatsApp Growth, monthly", "2000.00", "998314"),
        ("Extra seats (2)", "600.00", "998314"),
    ]
    assert inv["buyer"]["gstin"] == INDIA_GSTIN and inv["seller"]["gstin"] == "29AAACX1234A1ZT"
    assert inv["place_of_supply"] == "Maharashtra (27)" and inv["tax_note"] is None
    # The seats for the coming period are added after each charge.
    assert gw.rzp_addons[-1]["item"]["amount"] == 70800

    txs = (await owner.get(f"{API}/billing/transactions")).json()
    assert [(t["status"], t["amount"], t["method"]) for t in txs] == [("succeeded", "3068.00", "upi")]

    # The next renewal gets the next number. Invoices can't be altered.
    await send(client, "razorpay", gw.razorpay_charged(sub_id, 306800))
    await drain(expect="charged")
    numbers = sorted(i["number"] for i in (await owner.get(f"{API}/billing/invoices")).json())
    assert numbers[1].endswith("/000002")

    # Adding a seat mid-period bills the rest of the period; removing one bills nothing now.
    before = len(gw.rzp_addons)
    r = await owner.put(f"{API}/billing/subscription/seats", json={"extra_seats": 3})
    assert r.json()["extra_seats"] == 3 and len(gw.rzp_addons) == before + 1
    assert 34000 < gw.rzp_addons[-1]["item"]["amount"] <= 35400
    r = await owner.put(f"{API}/billing/subscription/seats", json={"extra_seats": 0})
    assert r.json()["extra_seats"] == 0 and len(gw.rzp_addons) == before + 1

    # Razorpay can cancel at the end of the period but not undo it.
    r = await owner.post(f"{API}/billing/subscription/cancel")
    assert r.json()["cancel_at_period_end"] is True
    assert gw.calls("POST", f"/v1/subscriptions/{sub_id}/cancel") == [{"cancel_at_cycle_end": 1}]
    assert (await owner.post(f"{API}/billing/subscription/resume")).json()["error"]["code"] == "cannot_resume"


async def test_webhook_signatures_and_unknown_events(
    register: RegisterFn, client: httpx.AsyncClient, gw: FakeGateways, su_conn: Any
) -> None:
    body, headers = gw.razorpay_webhook(
        "subscription.activated", {"subscription": {"entity": {"id": "sub_x"}}}
    )
    r = await client.post(
        f"{API}/webhooks/razorpay", content=body, headers={**headers, "X-Razorpay-Signature": "f" * 64}
    )
    assert r.status_code == 401
    r = await client.post(f"{API}/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "t=1,v1=00"})
    assert r.status_code == 401
    stale = gw.stripe_webhook("invoice.paid", {"id": "in_1"}, ts=1_600_000_000)
    assert (await send(client, "stripe", stale)).status_code == 401
    wrong = gw.stripe_webhook("invoice.paid", {"id": "in_1"}, secret="whsec_other")
    assert (await send(client, "stripe", wrong)).status_code == 401
    # Signed but about nobody we know: stored as ignored, nothing to process.
    assert (await send(client, "razorpay", (body, headers))).status_code == 200
    assert await drain() == []
    assert await su_conn.fetchval("SELECT status FROM billing_events") == "ignored"


async def test_failed_payment_grace_and_recovery(
    register: RegisterFn,
    client: httpx.AsyncClient,
    gw: FakeGateways,
    catalogue: dict[str, uuid.UUID],
    su_conn: Any,
) -> None:
    from app.modules.notifications.email import OUTBOX

    owner = await india_owner(register)
    sub_id = await razorpay_subscribe(owner, gw, catalogue["starter_inr"])
    payment = {
        "id": "pay_fail1",
        "entity": "payment",
        "amount": 118000,
        "currency": "INR",
        "status": "failed",
        "subscription_id": sub_id,
        "error_description": "Card declined",
        "notes": {},
    }
    await send(client, "razorpay", gw.razorpay_webhook("payment.failed", {"payment": {"entity": payment}}))
    await drain(expect="payment_failed")
    sub = (await owner.get(f"{API}/billing/subscription")).json()
    assert sub["status"] == "past_due" and sub["in_force"] is True and sub["grace_ends_at"]
    assert any("payment didn't go through" in m.subject for m in OUTBOX)
    assert (
        await owner.post(
            f"{API}/billing/subscription/change", json={"price_id": str(catalogue["growth_inr"])}
        )
    ).json()["error"]["code"] == "payment_due"

    # After the grace period the plan stops working until the payment is collected.
    await su_conn.execute(
        "UPDATE subscriptions SET past_due_since = now() - interval '8 days' WHERE status = 'past_due'"
    )
    sub = (await owner.get(f"{API}/billing/subscription")).json()
    assert sub["in_force"] is False
    usage = {u["key"]: u["limit"] for u in (await owner.get(f"{API}/billing/usage")).json()}
    assert usage["monthly_ai_messages"] == 0

    await send(client, "razorpay", gw.razorpay_charged(sub_id, 118000))
    await drain(expect="charged")
    sub = (await owner.get(f"{API}/billing/subscription")).json()
    assert sub["status"] == "active" and sub["in_force"] is True and sub["past_due_since"] is None
    statuses = sorted(t["status"] for t in (await owner.get(f"{API}/billing/transactions")).json())
    assert statuses == ["failed", "succeeded"]


# ---- Stripe ----------------------------------------------------------------------------------


async def test_stripe_checkout_export_invoice_and_changes(
    register: RegisterFn,
    client: httpx.AsyncClient,
    gw: FakeGateways,
    catalogue: dict[str, uuid.UUID],
    su_conn: Any,
) -> None:
    owner = await register("o@acme.com", "Acme Inc", country="US")
    r = await owner.put(
        f"{API}/billing/profile",
        json={
            "legal_name": "Acme Inc",
            "gstin": INDIA_GSTIN,
            "tax_id": "US-EIN-12-3456789",
            "state": "California",
        },
    )
    profile = r.json()
    assert profile["gstin"] is None and profile["state_code"] is None and profile["country"] == "US"

    r = await owner.post(f"{API}/billing/checkout", json={"price_id": str(catalogue["starter_usd"])})
    assert r.status_code == 201 and r.json()["stripe_url"].startswith("https://checkout.stripe.com/")
    cs_id = next(iter(gw.sessions))
    assert gw.sessions[cs_id]["metadata"]["tenant_id"] == owner.tenant_id
    sub = gw.stripe_pay(cs_id)

    # The invoice can arrive before the checkout event: it waits for the subscription.
    paid = gw.stripe_webhook("invoice.paid", gw.stripe_invoice(sub["id"]))
    await send(client, "stripe", paid)
    await drain(expect="retry")
    event_id = await su_conn.fetchval("SELECT id FROM billing_events WHERE type = 'invoice.paid'")

    # The return page confirms the session too; both paths are idempotent.
    r = await owner.post(f"{API}/billing/checkout/stripe/confirm", json={"session_id": cs_id})
    assert r.json()["status"] == "active" and r.json()["plan_name"] == "Starter"
    await send(client, "stripe", gw.stripe_webhook("checkout.session.completed", gw.sessions[cs_id]))
    await drain(expect="activated")
    other = await register("x@other.com", "Other", country="US")
    r = await other.post(f"{API}/billing/checkout/stripe/confirm", json={"session_id": cs_id})
    assert r.status_code == 404

    from app.modules.billing.events import process

    assert await process(event_id, uuid.UUID(owner.tenant_id)) == "paid"
    inv = (await owner.get(f"{API}/billing/invoices")).json()[0]
    inv = (await owner.get(f"{API}/billing/invoices/{inv['id']}")).json()
    assert (inv["currency"], inv["subtotal"], inv["tax_total"], inv["total"]) == (
        "USD",
        "15.00",
        "0.00",
        "15.00",
    )
    assert inv["taxes"] == [] and inv["tax_note"].endswith("under LUT AD290000000001")
    assert inv["buyer"]["tax_id"] == "US-EIN-12-3456789" and inv["place_of_supply"] is None

    # Upgrade: applies now, prorated and invoiced by Stripe.
    r = await owner.post(
        f"{API}/billing/subscription/change", json={"price_id": str(catalogue["growth_usd"])}
    )
    assert r.json()["immediate"] is True and r.json()["subscription"]["plan_name"] == "Growth"
    assert gw.subs[sub["id"]]["last_proration_behavior"] == "always_invoice"

    # Seats are a second item on the subscription.
    r = await owner.put(f"{API}/billing/subscription/seats", json={"extra_seats": 2})
    assert r.json()["extra_seats"] == 2
    items = gw.subs[sub["id"]]["items"]["data"]
    assert [i["quantity"] for i in items] == [1, 2]
    r = await owner.post(
        f"{API}/billing/subscription/change", json={"price_id": str(catalogue["starter_usd"])}
    )
    assert r.json()["error"]["code"] == "seats_not_offered"
    await owner.put(f"{API}/billing/subscription/seats", json={"extra_seats": 0})
    assert len(gw.subs[sub["id"]]["items"]["data"]) == 1

    # Downgrade: waits for the end of the paid period, even when Stripe reports the new
    # price straight away; it can be undone until then.
    r = await owner.post(
        f"{API}/billing/subscription/change", json={"price_id": str(catalogue["starter_usd"])}
    )
    out = r.json()
    assert out["immediate"] is False and out["subscription"]["plan_name"] == "Growth"
    assert out["subscription"]["pending_plan_name"] == "Starter"
    await send(
        client,
        "stripe",
        gw.stripe_webhook("customer.subscription.updated", gw.stripe_subscription_object(sub["id"])),
    )
    await drain(expect="customer.subscription.updated")
    assert (await owner.get(f"{API}/billing/subscription")).json()["plan_name"] == "Growth"
    r = await owner.delete(f"{API}/billing/subscription/pending-change")
    assert r.json()["pending_price"] is None
    growth_price = next(p for p in gw.prices.values() if p["unit_amount"] == "3000")
    assert gw.subs[sub["id"]]["items"]["data"][0]["price"]["id"] == growth_price["id"]

    # Downgrade again and let the period end: the sweeper applies it.
    await owner.post(f"{API}/billing/subscription/change", json={"price_id": str(catalogue["starter_usd"])})
    await su_conn.execute(
        "UPDATE subscriptions SET current_period_end = now() - interval '1 minute' WHERE status = 'active'"
    )
    from app.modules.billing.notices import sweep

    assert (await sweep())["applied"] == 1
    assert (await owner.get(f"{API}/billing/subscription")).json()["plan_name"] == "Starter"
    await su_conn.execute(
        "UPDATE subscriptions SET current_period_end = now() + interval '20 days' WHERE status = 'active'"
    )

    # Cancel at period end, then change their mind.
    r = await owner.post(f"{API}/billing/subscription/cancel")
    assert r.json()["cancel_at_period_end"] is True and gw.subs[sub["id"]]["cancel_at_period_end"] is True
    r = await owner.post(
        f"{API}/billing/subscription/change", json={"price_id": str(catalogue["growth_usd"])}
    )
    assert r.json()["error"]["code"] == "cancel_scheduled"
    r = await owner.post(f"{API}/billing/subscription/resume")
    assert r.json()["cancel_at_period_end"] is False and gw.subs[sub["id"]]["cancel_at_period_end"] is False

    # A failed renewal, then Stripe gives up and cancels.
    failed = gw.stripe_invoice(sub["id"], paid=False, attempt=1)
    await send(client, "stripe", gw.stripe_webhook("invoice.payment_failed", failed))
    await drain(expect="payment_failed")
    assert (await owner.get(f"{API}/billing/subscription")).json()["status"] == "past_due"
    gw.subs[sub["id"]]["status"] = "canceled"
    await send(
        client,
        "stripe",
        gw.stripe_webhook("customer.subscription.deleted", gw.stripe_subscription_object(sub["id"])),
    )
    await drain(expect="customer.subscription.deleted")
    sub_out = (await owner.get(f"{API}/billing/subscription")).json()
    assert sub_out["status"] is None and sub_out["in_force"] is False

    # With nothing live, the business can subscribe again.
    gw.sessions.clear()
    await stripe_subscribe(owner, gw, client, catalogue["starter_usd"])
    assert (await owner.get(f"{API}/billing/subscription")).json()["status"] == "active"


async def test_plan_must_fit_current_usage(
    register: RegisterFn, client: httpx.AsyncClient, gw: FakeGateways, catalogue: dict[str, uuid.UUID]
) -> None:
    from tests.integration.test_tenants import _join_new

    owner = await india_owner(register)
    for i in range(2):
        await _join_new(register, owner, f"agent{i}@acme.in", "agent")
    # Three members; Starter allows two.
    r = await owner.post(f"{API}/billing/checkout", json={"price_id": str(catalogue["starter_inr"])})
    err = r.json()["error"]
    assert (
        err["code"] == "plan_too_small" and "Team members: you use 3, the plan allows 2" in err["details"][0]
    )

    await razorpay_subscribe(owner, gw, catalogue["growth_inr"], seats=0)
    r = await owner.post(
        f"{API}/billing/subscription/change", json={"price_id": str(catalogue["starter_inr"])}
    )
    assert r.json()["error"]["code"] == "plan_too_small"
    # Yearly is an upgrade and applies at once.
    r = await owner.post(
        f"{API}/billing/subscription/change", json={"price_id": str(catalogue["growth_inr_year"])}
    )
    assert r.json()["immediate"] is True
    rzp = next(iter(gw.rzp_subs.values()))
    assert gw.calls("PATCH", f"/v1/subscriptions/{rzp['id']}")[-1]["schedule_change_at"] == "now"


async def test_billing_permissions(
    register: RegisterFn, client: httpx.AsyncClient, gw: FakeGateways, catalogue: dict[str, uuid.UUID]
) -> None:
    from tests.integration.test_tenants import _join_new

    owner = await india_owner(register)
    admin = await _join_new(register, owner, "admin@acme.in", "admin")
    agent = await _join_new(register, owner, "agent@acme.in", "agent")
    assert (await agent.get(f"{API}/billing/subscription")).status_code == 403
    assert (await admin.get(f"{API}/billing/subscription")).status_code == 200
    assert (await admin.get(f"{API}/billing/invoices")).status_code == 200
    r = await admin.post(f"{API}/billing/checkout", json={"price_id": str(catalogue["growth_inr"])})
    assert r.status_code == 403
    assert (await admin.put(f"{API}/billing/profile", json={"legal_name": "X"})).status_code == 403

    # Another business can't read this one's invoices.
    sub_id = await razorpay_subscribe(owner, gw, catalogue["growth_inr"])
    await send(client, "razorpay", gw.razorpay_charged(sub_id, 236000))
    await drain(expect="charged")
    inv_id = (await owner.get(f"{API}/billing/invoices")).json()[0]["id"]
    stranger = await india_owner(register, "s@else.in")
    assert (await stranger.get(f"{API}/billing/invoices/{inv_id}")).status_code == 404
    assert (await stranger.get(f"{API}/billing/invoices")).json() == []


async def test_trial_reminder_and_end(register: RegisterFn, su_conn: Any, database: Any) -> None:
    from app.modules.billing.notices import sweep
    from app.modules.notifications.email import OUTBOX

    owner = await register("owner@acme.in")
    sub = (await owner.get(f"{API}/billing/subscription")).json()
    assert sub["status"] == "trialing" and sub["in_force"] is True and sub["plan_code"] == "trial"
    assert (await sweep())["tenants"] == 0

    await su_conn.execute("UPDATE subscriptions SET trial_ends_at = now() + interval '2 days'")
    OUTBOX.clear()
    assert (await sweep())["applied"] == 1
    assert [m.subject for m in OUTBOX] == ["Your XploWhatsApp trial ends in 2 days"]
    assert (await sweep())["tenants"] == 0  # only once

    await su_conn.execute("UPDATE subscriptions SET trial_ends_at = now() - interval '1 minute'")
    # Access ends at the trial's end even before the sweeper runs.
    assert (await owner.get(f"{API}/billing/subscription")).json()["in_force"] is False
    assert (await sweep())["applied"] == 1
    assert OUTBOX[-1].subject == "Your XploWhatsApp trial has ended"
    assert "/billing" in OUTBOX[-1].text
    sub = (await owner.get(f"{API}/billing/subscription")).json()
    assert sub["status"] is None and sub["plan_code"] is None
    usage = {u["key"]: u["limit"] for u in (await owner.get(f"{API}/billing/usage")).json()}
    assert set(usage.values()) == {0}
