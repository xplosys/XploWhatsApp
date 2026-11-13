# Billing and subscriptions

How XploWhatsApp sells plans, collects payments and issues tax invoices. Phase 7.

## The model

| Table | What it holds | Who writes it |
|---|---|---|
| `subscription_plans` | Plan code, name, limits (`max_users`, `max_whatsapp_numbers`, `max_knowledge_documents`, `max_storage_mb`, `monthly_ai_messages`, `platform_ai_keys`), features, public and active flags, display order | Super Admin |
| `plan_prices` | One price per plan, currency and interval: amount before tax, optional per-seat amount, the tax rate at creation, the gateway and its plan or price IDs. Never edited; a new price retires the old one | Super Admin (the app role cannot write it) |
| `subscriptions` | One live subscription per business (`trialing`, `active` or `past_due`), its price, extra seats, period, a pending downgrade, cancellation and grace dates | App, gateway webhooks, sweeper |
| `billing_checkouts` | A checkout started on a gateway, so the confirmation can be matched to the price and seats chosen | App |
| `billing_profiles` | The business's legal name, address, state (GST state code), GSTIN or foreign tax ID | Owner |
| `billing_invoices` | One tax invoice per successful payment, with seller and buyer snapshots. The app role can only insert (no update or delete) | App |
| `billing_transactions` | Every payment attempt that the gateways report, succeeded or failed (no delete) | App |
| `billing_events` | The gateway webhook inbox: stored once per (gateway, event ID), then applied by the worker (no delete) | Webhook endpoint, worker |
| `invoice_sequences` | The last invoice number per financial year | Only `app_next_invoice_number()` |

All tenant tables have forced RLS. Prices, sequences and the cross-tenant lookups go through `SECURITY DEFINER` functions owned by `xplo_definer` that return IDs only.

## Gateways

`PaymentGateway` (`app/modules/billing/gateways/base.py`) has one implementation per provider. Both are thin httpx clients on the documented REST APIs with a transport hook used by the test fakes.

| | Razorpay (INR) | Stripe (USD, GBP, EUR) |
|---|---|---|
| Price | `POST /v1/plans` with the tax-inclusive amount | Product plus a base price and a seat price |
| Checkout | `POST /v1/subscriptions` (120 monthly or 10 yearly cycles, notes carry the business ID), then Razorpay Checkout in the page | Checkout Session in `subscription` mode, redirect, then back to `/billing?checkout=success&session_id=…` |
| Confirmation | Checkout returns `razorpay_signature` = HMAC-SHA256(`payment_id|subscription_id`, key secret); the API checks it and fetches the subscription | The return page calls `/billing/checkout/stripe/confirm`; the session's metadata must name the business |
| Upgrade | `PATCH` with `schedule_change_at=now` | Item price change with `proration_behavior=always_invoice` |
| Downgrade | `schedule_change_at=cycle_end`; undo with `cancel_scheduled_changes` | Item price change with no proration; undo puts the old price back |
| Extra seats | Add-ons. Adding seats mid-period adds a pro-rata add-on to the next invoice; each renewal adds one for the coming period | A second subscription item; Stripe prorates it |
| Cancel | `cancel_at_cycle_end=1`. Cannot be undone on Razorpay | `cancel_at_period_end`; can be resumed |
| Webhook signature | `X-Razorpay-Signature` (hex HMAC of the body with the webhook secret); event ID from `X-Razorpay-Event-Id` | `Stripe-Signature` `t` and `v1`, checked against a 5 minute tolerance |

India businesses see INR prices only; everyone else sees USD, GBP and EUR (`currencies_for`). Plan changes stay in the subscription's currency.

## Webhooks

`POST /api/v1/webhooks/razorpay` and `/stripe`:

1. Verify the signature (401 otherwise). Bodies over 1 MB are refused.
2. Find the business: through `app_billing_tenant(gateway, ref)` for a subscription, checkout or customer we know, otherwise the business ID the gateway carried back in the signed notes or metadata.
3. Store the event once (`billing_events`, unique on gateway and event ID) and return 200. Events that name no business are stored as `ignored`.
4. After commit, the worker applies it in a savepoint. An `invoice.paid` that arrives before the checkout event raises `RetryLater` and is tried again by the sweeper; after 8 attempts the event is marked `failed`.

| Event | Effect |
|---|---|
| Razorpay `subscription.activated` / `charged` | Activates the checkout if needed (ends the trial), syncs status and period. `charged` records the payment, issues the invoice and adds the next period's seat add-on |
| Razorpay `subscription.pending` / `halted` / `cancelled` / `completed` | Status sync (`past_due` or `canceled`) |
| Razorpay `payment.failed` | Records the failed payment, marks the plan `past_due`, emails owners and admins |
| Stripe `checkout.session.completed` | Fetches the subscription and activates it |
| Stripe `customer.subscription.*` | Status, period, cancellation and price sync. A downgrade that Stripe reports early stays pending until the paid period ends |
| Stripe `invoice.paid` | Records the payment and issues the invoice with Stripe's line items |
| Stripe `invoice.payment_failed` | As Razorpay `payment.failed`, once per attempt |

## Access rules

`effective_plan()` gives every limit check in the app the live subscription's plan. A plan stops giving access (limits become 0, so the assistant stops replying and nothing new can be added) when:

- the trial's end date has passed,
- a failed payment is older than `BILLING_GRACE_DAYS` (7) and still unpaid,
- a cancellation's period has ended.

This applies the moment the date passes, even before the sweeper records it. `max_users` is the plan's number plus extra seats. Data is never deleted when a plan ends.

Starting a checkout or changing plan checks that the plan fits what the business uses now (members, numbers, documents, storage) and lists what doesn't fit. Seats can't go below what the current members need.

## Tax invoices

- Numbered `{INVOICE_PREFIX}/{FY}/{n:06d}`, for example `XW/2026-27/000001`. The financial year runs April to March in Indian time. Numbers are consecutive per year, taken inside the payment's transaction from `app_next_invoice_number()`, so a rolled-back payment does not leave a gap.
- INR prices carry `GST_RATE_PERCENT` (18) when `SELLER_GSTIN` is set; the rate is frozen on the price when it is created. The invoice splits the tax-inclusive total into taxable value and tax: CGST and SGST in halves when the buyer's state equals `SELLER_STATE_CODE`, otherwise IGST. Place of supply is the buyer's state.
- Non-INR invoices from a GST-registered seller are zero-rated exports and say "Supply meant for export of services without payment of IGST under LUT …".
- The buyer's GSTIN is checked (format, state code and check digit) and sets the state.
- The invoice stores snapshots of seller and buyer, so later profile edits don't change issued invoices. The app role cannot update or delete invoices.
- Invoices are shown as a printable page (`/billing/invoices/{id}`, "Print or save as PDF"). There is no PDF library on the server.

**Confirm with your accountant before going live:** the SAC code (`INVOICE_SAC_CODE`), the GST rate, whether you hold an LUT, and the invoice series prefix.

## Sweeper

`sweep-billing` runs every 10 minutes:

- trial reminder email `BILLING_TRIAL_REMINDER_DAYS` (3) before the end, once;
- trial end: status `expired`, email;
- period end: applies a pending downgrade, or ends a cancelled plan;
- re-sends webhook jobs that are still pending after a minute (lost broker message, `RetryLater`).

## API

| Method and path | Who | What |
|---|---|---|
| `GET /billing/subscription` | owner, admin | Plan, status, period, pending change, grace end, seats |
| `GET /billing/usage` | owner, admin | Usage against each limit |
| `GET /billing/plans` | owner, admin | Public plans with the prices for the business's country; `payments_ready` |
| `GET /billing/states` | owner, admin | GST states and codes |
| `GET`, `PUT /billing/profile` | read: owner, admin; write: owner | Billing details |
| `POST /billing/checkout` | owner | Start a checkout; returns Razorpay options or a Stripe URL |
| `POST /billing/checkout/razorpay/verify` | owner | Razorpay Checkout's signed result |
| `POST /billing/checkout/stripe/confirm` | owner | Stripe session on return |
| `POST /billing/subscription/change` | owner | Change plan; says whether it applied now |
| `DELETE /billing/subscription/pending-change` | owner | Undo a scheduled downgrade |
| `PUT /billing/subscription/seats` | owner | Extra seats |
| `POST /billing/subscription/cancel`, `/resume` | owner | Cancel at period end; resume (Stripe) |
| `GET /billing/invoices`, `/invoices/{id}`, `/transactions` | owner, admin | History |
| `GET`, `POST /admin/billing/plans`, `PATCH /admin/billing/plans/{id}` | Super Admin | Plans and limits |
| `POST /admin/billing/plans/{id}/prices`, `DELETE …/prices/{price_id}` | Super Admin | Add (creates it on the gateway) or retire a price |

Every change is in the audit log.

## Setting up for real

1. Razorpay: activate Subscriptions on the account, create API keys, add the webhook (`/api/v1/webhooks/razorpay`, events `subscription.*` and `payment.failed`) and copy its secret.
2. Stripe: create a restricted or secret key, add the webhook endpoint (`/api/v1/webhooks/stripe`, events `checkout.session.completed`, `customer.subscription.created`, `.updated`, `.deleted`, `invoice.paid`, `invoice.payment_failed`) and copy its signing secret.
3. Fill the seller settings and the variables above in `.env`, restart the API and worker.
4. In the Super Admin console, create the plans and add prices. Each price is created on the gateway at once; a gateway error stores nothing.
5. Make a test purchase in each gateway's test mode before switching keys to live.

## Not in this phase

- Refunds and credit notes (issue them in the gateway dashboard; a credit note flow comes later).
- Coupons and discounts.
- Paying WhatsApp conversation charges through XploSys (businesses pay Meta directly, ADR-009).
- Changing currency on an existing subscription (cancel and subscribe again).
