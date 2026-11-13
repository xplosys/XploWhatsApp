# Launch checklist

What must be true before XploWhatsApp takes its first outside business, in the order to do it.
Tick each line on the day. Items marked **(XploSys)** are business or legal steps that no
code can do; everything else is a command or a screen in the product.

Related guides: [DEPLOYMENT_UBUNTU.md](DEPLOYMENT_UBUNTU.md) (server),
[META_INTEGRATION.md](META_INTEGRATION.md) (Meta app), [SECURITY.md](SECURITY.md),
[BILLING.md](BILLING.md), [../e2e/README.md](../e2e/README.md) (end-to-end and load tests).

## 1. Release candidate

- [ ] CI is green on the commit you will deploy: backend, frontend, docker, security, image
      scan, deploy config, and the **e2e** job (the full customer journey and the handover
      to a person, run against the real containers).
- [ ] The load test passes on the production-sized server before launch (section 7), with
      `E2E_AI_LATENCY_MS` set to the chat model's real latency.
- [ ] Migrations in this release work with the previous release's code (add, backfill, drop
      later), so a rollback is safe.
- [ ] `META_GRAPH_API_VERSION`, the Embedded Signup version and the webhook fields are
      re-checked against Meta's documentation (they change often; see META_INTEGRATION.md).

## 2. Server and services

Follow DEPLOYMENT_UBUNTU.md sections 1 to 9, then confirm:

- [ ] `https://app.xplowhatsapp.com/health/ready` answers 200, and the certificate renews
      (`certbot renew --dry-run`).
- [ ] `.env` has `ENVIRONMENT=production`, `COOKIE_SECURE=true`, real SMTP, and secrets made
      by `python -m app.cli generate-secrets` (never copied from another environment).
- [ ] `ENCRYPTION_KEYS` and the backup private key are stored offline in two places.
- [ ] ClamAV is running (`docker compose ps clamav` healthy): while it is configured but
      down, uploaded documents fail with "scanner unavailable" instead of being read.
- [ ] Workers: `worker` (webhooks, sends, knowledge) and `worker-ai` (AI replies) both
      healthy. `AI_WORKER_CONCURRENCY` is set from section 7.
- [ ] A backup has run, been copied off-site, and the weekly restore check has passed
      (Grafana "server" dashboard: last backup).
- [ ] Alert email works: stop a service briefly and receive the ServiceDown email, then
      the resolved email.
- [ ] Sentry receives an API error and a browser error tagged with this release.

## 3. Platform set-up (Super Admin)

- [ ] Create the Super Admin: `docker compose exec api python -m app.cli create-super-admin <email>`.
      Sign in and turn on two-factor at once (admin pages need it).
- [ ] AI models: Admin → AI models → Sync for each provider whose platform key is set.
      Add a price to every chat model you switch on (estimates on the usage page come from
      these prices), then switch on one chat model and one embedding model per provider.
- [ ] Plans: Admin → Plans. Check limits (`monthly_ai_messages`, numbers, seats, storage)
      and add the live prices. Each price creates the matching Razorpay or Stripe plan.
- [ ] Payments use **live** keys, and the gateway webhooks point at
      `https://app.xplowhatsapp.com/api/v1/webhooks/razorpay` and `.../webhooks/stripe`
      with their secrets in `.env`.
- [ ] Seller details for invoices (`SELLER_*`, `GST_RATE_PERCENT`, `INVOICE_PREFIX`) are
      correct; make one real low-value payment and check the tax invoice, then refund it.

## 4. Meta

The order and the owners are in META_INTEGRATION.md section 2. Before launch:

- [ ] **(XploSys)** Business verification of XploSys is approved.
- [ ] **(XploSys)** The app is a Business app owned by the XploSys portfolio, with WhatsApp
      added, and app basics filled in: icon, category, contact email, privacy policy URL,
      terms URL and data deletion instructions URL (section 6).
- [ ] Webhook: callback `https://app.xplowhatsapp.com/api/v1/webhooks/whatsapp`, the verify
      token from `.env`, fields `messages`, `account_update`,
      `message_template_status_update`, `template_category_update` and
      `phone_number_quality_update`.
- [ ] Facebook Login for Business: an Embedded Signup configuration (its ID in
      `META_EMBEDDED_SIGNUP_CONFIG_ID`), `app.xplowhatsapp.com` in Allowed Domains and
      Valid OAuth Redirect URIs.
- [ ] The WhatsApp page in the app shows a Connect button (it lists any missing setting
      instead).
- [ ] **(XploSys)** App Review approved for `whatsapp_business_management` and
      `whatsapp_business_messaging` with Advanced Access (section 5), and Tech Provider
      onboarding complete.

Until approval, only people with a role on the Meta app can connect, and only Meta's test
number and its registered recipients work. That is enough to record the screencast.

## 5. App Review submission pack

Fill the App Review form from this section. Meta's form changes; check the wording it asks
for on the day, and keep answers to what the product really does.

### Permissions to request

Request only these two. XploWhatsApp makes no call that needs `business_management`, so do
not request it unless Meta's form says Embedded Signup for Tech Providers now requires it.

| Permission | What XploWhatsApp does with it | Calls (all server to server, with the business's own token from Embedded Signup) |
|---|---|---|
| `whatsapp_business_management` | Connects a business's WhatsApp Business Account after Embedded Signup: confirms the account and number it chose, subscribes our app to its webhooks, registers the number for the Cloud API, checks the connection's health daily, and lists its approved message templates so staff can send them. Disconnecting unsubscribes the app. | `GET /debug_token`, `GET /{waba_id}`, `GET /{waba_id}/phone_numbers`, `POST/GET/DELETE /{waba_id}/subscribed_apps`, `POST /{phone_number_id}/register`, `GET /{waba_id}/message_templates` |
| `whatsapp_business_messaging` | Receives customers' messages through the webhook and sends the business's replies: answers from the business's AI assistant (grounded only in the business's own information, with a hand-over to a person) and messages typed by its staff in the shared inbox. Free-form messages only inside the 24-hour customer service window; templates outside it. | Webhook `messages`; `POST /{phone_number_id}/messages` |

Suggested text for "How will your app use this permission?":

> **whatsapp_business_management:** XploWhatsApp is a customer-service inbox and AI
> assistant for small businesses. A business owner connects their own WhatsApp Business
> Account with Embedded Signup. We use this permission, with the token Embedded Signup
> returns for that business, to confirm the account and phone number they selected,
> subscribe our app to that account's webhooks, register the number for the Cloud API,
> check the connection daily so we can tell the owner if it breaks, and show the business's
> approved message templates in our inbox. We never manage accounts the business did not
> connect, and the owner can disconnect at any time.
>
> **whatsapp_business_messaging:** We receive the messages that customers send to the
> connected business number and show them in the business's inbox. Replies are sent on the
> business's behalf, either by its staff from the inbox or by its AI assistant, which answers
> only from information the business provided and hands the conversation to a person when it
> cannot help. We send free-form messages only within the 24-hour customer service window and
> approved templates outside it, and we honour opt-outs.

### Screencast script (about 4 minutes, one take, English captions)

Record on the production domain with a test business (a person with a role on the Meta app)
and Meta's test number. Show the browser address bar throughout.

1. **Sign up** at `https://app.xplowhatsapp.com/register`: business name, owner, email,
   password. (0:00 to 0:20)
2. **Connect WhatsApp:** WhatsApp → Connect. Meta's Embedded Signup window opens; pick the
   business portfolio, the WhatsApp Business Account and the number; finish. Back in the
   app the number shows as connected. Caption: "whatsapp_business_management: confirm the
   account, subscribe to webhooks, register the number." (0:20 to 1:20)
3. **Templates:** WhatsApp → Templates shows the account's approved templates. Caption:
   "whatsapp_business_management: read message templates." (1:20 to 1:35)
4. **Set up the assistant:** AI assistant → choose the model, write "About your business"
   and opening hours, save; Knowledge → upload a one-page price list PDF and wait for
   Ready; turn on automatic replies. (1:35 to 2:20)
5. **Receive and reply:** from a registered test phone, send "How much is a scale and
   polish?" to the test number. Show the conversation arriving in Conversations, the AI
   reply quoting the price from the PDF with its source, and the same reply on the phone.
   Caption: "whatsapp_business_messaging: receive a message by webhook and send a reply."
   (2:20 to 3:10)
6. **A person takes over:** send "I want to speak to someone." The conversation shows
   "Needs a person"; type a reply in the inbox, send, show it on the phone, then
   "Hand back to AI". (3:10 to 3:50)
7. **Disconnect** (optional): WhatsApp → Disconnect, to show the business stays in control.

The automated E2E tests follow the same steps against fake Meta services
(`e2e/tests/journey.spec.ts`, `handover.spec.ts`), so rehearse with them first.

### Reviewer instructions and test account

- [ ] A reviewer login (email and password) on production, with two-factor off for that
      account only, and a business already connected to the test number. Give these in the
      form's private test-credentials field, never in the screencast.
- [ ] Steps for the reviewer: sign in, open Conversations, send a message to the test number
      from a number Meta lists as a recipient, watch the reply arrive.
- [ ] Delete the reviewer account after approval.

### Data handling answers

- Data we receive through these permissions: WhatsApp Business Account and number details,
  message templates, customers' messages and profile names. Stored in Postgres in the
  business's own tenant (row-level security), tokens encrypted (AES-GCM, rotatable keys).
- Shared with: the AI provider the business chose (the message, the conversation's recent
  history and passages from the business's own documents), only to write the reply. Nothing
  is sold or used for advertising.
- Retention: the business chooses how long messages are kept (30 days to 2 years). Raw
  webhook payloads are deleted after 30 days. Deleting a business removes its data after
  30 days, except invoices and the audit log, which the law requires us to keep.
- Security: see SECURITY.md (encryption, two-factor, audit log, rate limits, pen-test list).

## 6. Legal and privacy

- [ ] **(XploSys)** Privacy policy published (for example `https://xplosys.com/privacy`). It
      must name what section 5 lists: the data, the AI providers as processors, retention,
      and how to ask for deletion. Have it reviewed by counsel for the DPDP Act 2023, and for
      GDPR if you sell in the EU or UK.
- [ ] **(XploSys)** Terms of service published, including acceptable use that forbids
      spam and follows WhatsApp's Business and Commerce policies.
- [ ] **(XploSys)** Data deletion instructions published. Factual content you can use:
      - A business owner deletes the business and all its data from Settings → Privacy &
        security → Delete this business. Data is removed after 30 days; tax invoices,
        payment records and the audit log are kept as the law requires.
      - A business can export everything first (Privacy & security → Export your data).
      - A customer who messaged a business can ask that business to forget them; an
        owner or admin opens Contacts → the contact → Erase, which deletes the contact,
        their conversations, messages, notes and leads at once. Customers can also write
        to the support address, which is passed to the business.
      - A customer who sends STOP gets no further messages, and the assistant stops.
- [ ] **(XploSys)** A data processing agreement for businesses that ask for one, and a
      list of sub-processors (hosting, object storage, email, AI providers, payment
      gateways, Sentry).
- [ ] **(XploSys)** A grievance officer and contact for the DPDP Act, named in the privacy
      policy.

## 7. Capacity

Measured with `e2e/load/whatsapp.js` on a 4 vCPU, 16 GB machine running everything (API with
2 processes, `worker` with 4, `worker-ai` with 12, Postgres, Redis), with dashboards
polled at 10 requests a second throughout. All customers got a reply in every run.

| Customer messages a second | AI latency | Reply time p50 / p95 | Webhook answered p95 | Dashboard p95 (slowest: overview) |
|---|---|---|---|---|
| 10 | instant | 1.15 s / 1.19 s | 27 ms | under 100 ms |
| 20 | instant | 7.7 s / 11.3 s (CPU full) | 50 ms | under 800 ms |
| 3 | 2 s, AI sharing the message queue (before the fix) | 35 s / 58 s | 25 ms | under 100 ms |
| 3 | 2 s, separate AI workers | 3.1 s / 3.2 s | 24 ms | under 100 ms |
| 5 | 2 s, separate AI workers | 3.1 s / 3.2 s | 26 ms | 76 ms |
| 8 | 2 s, separate AI workers | 20 s / 36 s (AI workers full) | 23 ms | 92 ms |

How to size it:

- **AI replies a second ≈ `AI_WORKER_CONCURRENCY` ÷ the model's reply time.** With 12
  processes and a 2-second model, about 6 a second; each AI process holds a provider call
  open and uses about 150 MB. Raise `AI_WORKER_CONCURRENCY` (and memory) before raising
  anything else.
- Each message also costs about 150 ms of CPU across the webhook, the reply task and the
  send. One 4 vCPU server handles about 10 messages a second at steady state.
- Webhooks are answered in tens of milliseconds at every rate tested, because they are only
  stored and queued; Meta's retry deadline is never at risk.
- Bursts beyond capacity queue up and are all answered; the "Queue backlog" alert fires if
  they don't drain.

Targets the load test enforces (k6 fails the run otherwise): webhook answered p95 under
500 ms and p99 under 1 s, no webhook rejected, each dashboard read p95 under 800 ms, under
1% dashboard errors, every customer answered within the drain time, and reply p95 under
`REPLY_P95_SECONDS` (10 s by default; add the model's own latency for production).

Measure right after a deploy settles, not during it: the first requests after a restart
are slower while processes warm up.

## 8. Launch day

- [ ] Deploy the release tag (Release workflow or `deploy/scripts/deploy.sh <tag>`), note
      the previous tag for rollback.
- [ ] Smoke test on production with the test business: sign in, inbox loads, send a message
      from a test phone, receive the AI reply, take over and hand back, upload a document,
      check usage and the overview.
- [ ] Watch Grafana for the first hour: errors, latency, queue depth, WhatsApp send failures,
      AI provider failures.
- [ ] Open sign-ups (share the register link) only after the smoke test passes.

### Rollback

Roll back when the smoke test fails, the error rate alert fires and a fix is not minutes
away, or messages stop being answered.

1. Run the Release workflow with the previous tag (or `deploy/scripts/deploy.sh <previous tag>`
   on the server). Containers are swapped; data stays.
2. If a migration must be undone, restore the encrypted dump the deploy took before
   migrating (DEPLOYMENT_UBUNTU.md, "Restoring"). Messages received since then are lost from
   our side, so prefer a forward fix when the schema change was additive.
3. If Meta webhooks failed meanwhile, Meta retries them for some hours; check the queue drains.
4. Tell connected businesses by email if replies were delayed for more than 15 minutes.

## 9. First week

- [ ] Daily: Grafana app dashboard, Sentry new issues, failed sends, WhatsApp connections
      that need reconnecting (Admin → Businesses).
- [ ] Check the AI usage page against the providers' own bills; adjust model prices if the
      estimates drift.
- [ ] Re-run the load test after any change to worker counts or the server size.
- [ ] Review the quality rating and messaging limit of connected numbers in Meta's tools.
