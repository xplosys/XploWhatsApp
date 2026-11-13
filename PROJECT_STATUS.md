# XploWhatsApp — project status

**Last updated:** 2026-11-13
**Current phase:** Phase 10 complete (production testing and release). All ten phases are built; launch waits on review and Meta App Review

## Phase tracker

| Phase | Status | Notes |
|---|---|---|
| 0. Architecture and planning | Done (approved 2026-11-12) | ARCHITECTURE.md, docs/META_INTEGRATION.md, docs/IMPLEMENTATION_PLAN.md |
| 1. Foundation | Done (PR #1) | See Phase 1 report |
| 2. Meta WhatsApp integration | Done (PR #2) | See Phase 2 report. Going live needs Meta App Review and Tech Provider approval |
| 3. AI provider orchestration | Done (PR #3) | See Phase 3 report and docs/AI_PROVIDERS.md |
| 4. RAG knowledge engine | Done (PR #4) | See Phase 4 report and docs/KNOWLEDGE.md |
| 5. Conversation management | Done (PR #5) | See Phase 5 report and docs/CONVERSATIONS.md |
| 6. Customer dashboard | Done (PR #6) | See Phase 6 report and docs/ANALYTICS.md |
| 7. Billing and subscriptions | Done, awaiting review (PR #7) | See Phase 7 report and docs/BILLING.md |
| 8. Security hardening | Done, awaiting review (PR #8) | See Phase 8 report and docs/SECURITY.md |
| 9. Monitoring and deployment | Done, awaiting review (PR #9) | See Phase 9 report and docs/DEPLOYMENT_UBUNTU.md |
| 10. Production testing and release | Done, awaiting review (PR #10) | See Phase 10 report, docs/LAUNCH_CHECKLIST.md and e2e/README.md |

## Phase 10 report

### What is complete

- **End-to-end tests (ADR 061):** Playwright drives the real product in Chromium against the real API, workers, web app, Postgres, Redis and object storage. Only Meta's Graph API and the AI providers are replaced, by `e2e/fakes/server.py`, which serves the backend's own test fakes over HTTP and sends customer messages as signed Meta webhooks. Facebook's SDK is stubbed in the page. Two journeys:
  - **register → connect WhatsApp → Gemini → upload a PDF → customer message → AI reply → usage → dashboard.** It checks the AI call carried the PDF's passage, the reply shows its source, the reply left through the WhatsApp API to the customer's number, usage lists the model, and the overview counts one customer message handled by the assistant alone;
  - **handover:** the assistant hands a complaint to the team, the owner replies from the inbox (sent through the WhatsApp API) and hands it back, and the assistant answers the next message.
- **E2E in CI:** a new `e2e` job builds the stack with `e2e/docker-compose.e2e.yml`, creates the Super Admin with the CLI, runs both journeys, and on failure prints the page snapshots and service logs and uploads screenshots and traces.
- **Load tests (ADR 063):** a k6 script (`e2e/load/whatsapp.js`) sends new customers' messages at a set rate while the dashboards poll the inbox counts, conversation list, overview and current user. It fails the run if a webhook is answered slower than 500 ms at p95 (1 s at p99), a dashboard read is slower than 800 ms at p95, any customer goes unanswered, or reply p95 exceeds the target. The fake service measures each reply from the customer's message to the WhatsApp API call. A Playwright step prepares the business through the UI.
- **Fix found by the load test (ADR 062):** AI replies shared the `messages` queue with webhooks and sends. With a model that takes 2 seconds, four worker processes spent their time waiting on the provider: at 3 messages a second, replies took 35 s at the median and 58 s at p95. AI replies and summaries now have their own `ai` queue and `worker-ai` service (`AI_WORKER_CONCURRENCY`, default 12), with its own health check and metrics target. The same load now gets replies in 3.1 s (p50) and 3.2 s (p95). A test checks the routing and that both compose files consume exactly the queues the metrics know.
- **docs/LAUNCH_CHECKLIST.md:** the go-live list in order:
  - release candidate and server checks;
  - platform set-up (Super Admin, models and prices, plans, live payment keys);
  - Meta set-up;
  - **App Review submission pack:** the two permissions to request and why (`business_management` is not needed), suggested answers, a timed screencast script, reviewer instructions and data-handling answers;
  - legal and privacy items for XploSys (privacy policy, terms, data deletion instructions with factual text, DPA, DPDP grievance officer);
  - measured capacity and how to size workers;
  - launch day smoke test, rollback and the first week.
- **e2e/README.md:** how to run the E2E and load tests with Docker or against your own processes, and every setting.
- **Fixed along the way:**
  - the restore command stopped the worker but would have left the new AI worker writing to the database during a restore;
  - the README's no-Docker worker command would not have run AI replies.

### Tests

- Backend: 316 tests, all passing (1 new: AI tasks go to their own queue, and the compose files consume exactly the known queues).
- E2E: both journeys pass locally (about 26 s together) and in CI against the containers (36 s). The first CI run failed twice: the usage check matched a hidden option in the model picker (fixed by looking in the usage table), and the handover test never saw the "Turn on automatic replies" button. That second failure has not recurred in CI or in local runs, and its cause was not found from the logs; CI now prints the page snapshot of any failed test, so a repeat will show what the page held.
- Load, on a 4 vCPU, 16 GB sandbox running everything, with dashboards polled at 10 requests a second. Every customer was answered in every run:

  | Messages a second | AI latency | Reply p50 / p95 | Webhook p95 | Dashboard p95 |
  |---|---|---|---|---|
  | 10 | instant | 1.15 s / 1.19 s | 27 ms | under 100 ms |
  | 20 | instant | 7.7 s / 11.3 s (CPU full) | 50 ms | under 800 ms |
  | 3 | 2 s, before the queue split | 35 s / 58 s | 25 ms | under 100 ms |
  | 5 | 2 s, after the split | 3.1 s / 3.2 s | 26 ms | 76 ms |
  | 8 | 2 s, after the split | 20 s / 36 s (AI workers full) | 23 ms | 92 ms |

  So one such server takes about 10 messages a second of CPU, and AI replies a second are about `AI_WORKER_CONCURRENCY` divided by the model's reply time.
- `ruff`, `mypy --strict`, `eslint`, `tsc` (frontend and e2e) and `shellcheck`: clean.

### Not verified here

- Real Meta and real AI providers: E2E uses fakes, by design. The screencast for App Review (launch checklist section 5) is the end-to-end run against the real Meta test number.
- Load on the production server: the figures above come from the sandbox. Run the load test on the real server before launch, with `E2E_AI_LATENCY_MS` set to the chosen model's latency.
- App Review itself, business verification and Tech Provider onboarding: XploSys's steps with Meta.

### Known limitations of Phase 10

- Knowledge ingestion still shares the core worker with webhooks and sends, so a large batch of uploads can slow replies leaving. It could get its own queue the same way as AI if that shows up.
- The first requests after a restart are slower while processes warm up (one run right after an API restart saw a dashboard p95 of 5.5 s; the rerun was under 100 ms).
- The load test covers new conversations with text messages; media messages, template sends and billing webhooks are not in it.
- Privacy policy, terms and the data deletion page are XploSys's to publish; the checklist gives the content they must cover.

## Phase 9 report

### What is complete

- **Metrics (ADR 055):** Prometheus metrics for every API route (requests, status codes, latency), every background task (runs, outcome, duration), every AI provider call (result, latency) and queue depth. Labels are bounded: no IDs, phone numbers or business names. `/metrics` needs a token outside development and is never served publicly.
- **Dashboards:** two Grafana dashboards, provisioned from code:
  - "XploWhatsApp: app": traffic, errors, latency, slowest routes, queue, WhatsApp sends, AI replies and provider calls;
  - "XploWhatsApp: server": CPU, memory, disk, last backup, Postgres and Redis.
- **Alerts:** 15 rules, emailed through the existing SMTP settings:
  - a service down;
  - API errors or slowness;
  - a queue backlog;
  - failing WhatsApp sends;
  - failing tasks or AI providers;
  - missing backups, off-site copies or restore checks;
  - disk, memory, Postgres connections and Redis memory.

  The rules have unit tests (promtool).
- **Sentry (ADR 056):** errors from the API, the workers and browsers, tagged with the release. No request bodies, cookies, auth headers, user details or local variables are sent. Browser reports go through the app's own `/monitoring` path, which forwards only to the configured project.
- **Production stack (ADR 057):** `docker-compose.prod.yml` runs everything on one server from CI-built images. Monitoring and self-hosted storage are optional profiles, and only Nginx is exposed to the internet.
- **Nginx and TLS (ADR 058):**
  - Let's Encrypt certificates with automatic renewal;
  - HTTP redirected to HTTPS, with HSTS sent once;
  - per-IP rate limits;
  - the files domain is download-only.
- **Security fix:** the API container trusted `X-Forwarded-For` from anyone, so a client could pick its own IP and slip past the per-IP sign-in limits. Nginx now overwrites those headers, and the API ignores forwarded headers.
- **Backups (ADR 059):**
  - nightly `pg_dump` encrypted to a GPG key whose private half stays off the server;
  - an off-site copy to any S3-compatible storage;
  - a weekly automatic restore check;
  - a one-command restore;
  - systemd timers.
- **Deploys (ADR 060):** every push to `main` builds and pushes the images, then deploys over SSH:
  1. an encrypted dump;
  2. migrations;
  3. a restart and health check;
  4. an automatic rollback to the previous release if the check fails.

  A manual run with an older tag rolls back.
- **docs/DEPLOYMENT_UBUNTU.md:** a step-by-step guide covering:
  - server setup and firewall;
  - DNS, `.env` and object storage;
  - backup keys, certificates and the first deploy;
  - GitHub deploy settings;
  - backups and restore;
  - monitoring;
  - routine operations.

### Tests

- Backend: 315 tests, all passing (4 new). They cover:
  - metrics access (open in development, 404 in production without the token);
  - route templates without IDs;
  - task result labels;
  - Sentry scrubbing.
- `ruff`, `mypy --strict`, `eslint`, `tsc`, `next build`, `alembic check`, `shellcheck` and the promtool rule tests: clean.
- `deploy/scripts/test-nginx.sh` (also in CI) checks 10 behaviours of the real Nginx config:
  - HTTPS redirect;
  - HSTS;
  - hidden `/metrics` and health endpoints;
  - spoofed `X-Real-IP` and `X-Forwarded-For` replaced;
  - download-only files domain;
  - unknown hosts refused.
- A full production rehearsal on this machine, with `ENVIRONMENT=production`, both domains over HTTPS (self-signed certificates) and every service from the production compose file. Screenshots are in the project files under `phase9/`. It confirmed:
  - registration and the dashboard over HTTPS;
  - a spoofed IP ignored;
  - the refresh cookie Secure, HttpOnly and SameSite=Strict;
  - all Prometheus targets up and both dashboards showing live data;
  - an encrypted backup with no readable text;
  - a restore check passing;
  - an off-site copy landing in the bucket;
  - a real restore removing data added after the dump, with the app back up afterwards;
  - stopping the worker sending a ServiceDown email within three minutes, and a resolved email after restart.
- Found and fixed in the rehearsal:
  - the deploy job's `tar` would have failed (option order);
  - the monitoring stack would never have started (no `COMPOSE_PROFILES`);
  - alert email links pointed at container IDs;
  - CI's shellcheck step would have failed;
  - "Server errors" read "No data" instead of 0.

### Not verified here

- Building and pushing the images, and the SSH deploy itself: they run in GitHub Actions on the first push to `main`. This sandbox can't install packages inside `docker build`, so the rehearsal ran the same code on stock Python and Node images.
- Real Let's Encrypt certificates: they need a public domain.
- node-exporter's host mount with `rslave`: this sandbox lacks a shared root mount, and normal Ubuntu has one.
- Sentry delivery to a real project: the tunnel's checks are tested, but no DSN exists yet.

### Known limitations of Phase 9

- One server: a hardware failure means restoring onto a new one. The nightly dump bounds data loss at about a day; WAL archiving or a managed Postgres would shrink that.
- Alerts are email only. Slack or PagerDuty is one more Alertmanager receiver.
- Grafana, Prometheus and Alertmanager are reached through an SSH tunnel, not a public login.
- Logs stay on the server (rotated, 100 MB per container). There is no central log search.

## Phase 8 report

### What is complete

- **Sessions (ADR 046):** an access token works only while its sign-in is live, so signing a device out takes effect at once. Account & security lists every signed-in device with "Sign out" and "Sign out other devices". Turning two-factor on starts a fresh two-factor-verified sign-in.
- **Two-factor for the whole team (ADR 047):** an owner can require it in Privacy & security; members without it see a notice and can only reach their account page. Using a recovery code is audited. Super Admin already required it.
- **Rate limits and client IP (ADR 048):** per-person limit on every signed-in request, per-sender limit on webhooks, sign-in limits per IP; `X-Real-IP` is believed only from `TRUSTED_PROXIES`.
- **Headers:** a strict CSP, Permissions-Policy, COOP and CORP on the API, plus HSTS outside development. The web app uses a per-request nonce CSP with `'strict-dynamic'` (ADR 054); Razorpay Checkout was checked to work under it.
- **Key rotation (ADR 052):** `rotate-jwt-key`, `new-encryption-key` and `rewrap-secrets [--dry-run]`, with a runbook in docs/SECURITY.md.
- **Audit coverage (ADR 053):** every route that changes data is audited or listed with a reason; seven routes gained audit entries (number registration, template sync, notes, knowledge retries, unanswered questions, sessions).
- **Prompt injection (ADR 049):** prompt-frame imitation is removed from business settings and documents; prices, discounts and links must come from the business, not the customer; invisible characters can't hide a leak.
- **Privacy tools (ADRs 050, 051):** contact erasure; full data export as a zip of JSON Lines (owner, emailed, 7-day link, audited downloads); business deletion with a 30-day restore window, Super Admin restore, then a purge that keeps only invoices, payments and the audit log; account self-deletion; per-business message retention; webhook payloads removed after 30 days. Migration 0007.
- **CI:** pip-audit, pnpm audit, gitleaks over the full history, and Trivy on both images.
- **Fix found in the walkthrough:** a gateway reply that wasn't JSON (a proxy's error page) crashed checkout with a 500; it is now a clean "gateway error".
- **docs/SECURITY.md:** threat model, controls, key rotation, incident response with DPDP and GDPR notification timelines, pen-test checklist, and the data tools.

### Tests

- Backend: 311 tests, all passing (54 new). They cover:
  - sessions signed out remotely;
  - two-factor required by a business, and turning it off refused;
  - per-person and webhook rate limits, and spoofed forwarded IPs;
  - security headers;
  - contact erasure;
  - data export and its contents;
  - business deletion, restore and purge;
  - a paid plan blocking deletion;
  - account deletion;
  - message retention and webhook payload cleanup;
  - JWT rotation keeping issued tokens valid;
  - rewrapping every secret, and the all-or-nothing refusal;
  - the audit coverage check;
  - upload abuse: path traversal names, disguised files, missing sizes, downloads forced as attachments, and documents posing as instructions;
  - prompt injection: frame breaking, customer-supplied prices and links, leaks hidden with invisible characters;
  - non-JSON gateway errors.

  RLS tests include `data_exports`.
- `ruff`, `mypy --strict`, `eslint`, `tsc`, `next build` and `alembic check`: clean. pip-audit and pnpm audit: no known vulnerabilities.
- Browser walkthrough against the running stack (screenshots in the project files under `phase8/`):
  - signed-in devices and a remote sign-out;
  - privacy page before and after two-factor;
  - an agent blocked until they set up two-factor;
  - contact erasure;
  - export ready and downloaded;
  - Razorpay Checkout under the CSP;
  - deleting a business and the Super Admin restore list;
  - dark mode;
  - a phone at 390 px with no sideways scrolling.

  No CSP violations were reported.

### Not verified here

- gitleaks and Trivy run only in CI; this sandbox couldn't fetch them. pip-audit and pnpm audit ran here.
- No external penetration test yet; the checklist in docs/SECURITY.md is ready for one.

### Known limitations of Phase 8

- Key rotation is a manual runbook, not automatic.
- The breached-password check (HIBP) is not built.
- Two-factor is by authenticator app only; there are no passkeys or SMS codes.
- Business deletion keeps the tenant row, anonymised, so invoice numbers and the audit log stay valid.

## Phase 7 report

### What is complete

- **Data model (migration 0006):** `plan_prices` (catalogue, read-only for the app role), `billing_checkouts`, `billing_profiles`, `billing_invoices` (insert only for the app role), `billing_transactions` and `billing_events` (no delete), `invoice_sequences` (written only by a definer function). Subscriptions gain price, customer, pending downgrade, grace, cancellation and end dates. Forced RLS on the five new tenant tables; `billing_events` allows rows with no business so unroutable webhooks are kept.
- **Gateways:** Razorpay (INR) and Stripe (USD, GBP, EUR) behind `PaymentGateway`, as httpx clients on the documented REST APIs: price creation, checkout, plan changes, seats, cancel and resume, and signature checks for webhooks and Razorpay Checkout.
- **Subscriptions:** trial at registration, checkout that checks billing details and that the plan fits current usage, activation from the checkout callback or the webhook (whichever is first), upgrades now with proration, downgrades at period end (undoable), extra seats, cancel at period end, resume on Stripe, failed payments with a 7-day grace period, and access that ends by date even before the sweeper runs.
- **Webhooks:** stored once, acknowledged, applied by the worker; out-of-order Stripe events are retried; unknown events are kept as ignored.
- **Tax invoices:** consecutive numbers per Indian financial year, GST split into CGST and SGST or IGST by the buyer's state, GSTIN validation with the check digit, export note with LUT for non-INR invoices, seller and buyer snapshots, printable invoice page.
- **Emails:** trial ending (3 days before), trial ended, payment failed; to owners, admins and the billing email.
- **Super Admin:** create and edit plans (limits, features, order, on sale or hidden, on or off), add prices (created on the gateway at once, tax rate frozen, old price retired), retire prices, subscriber counts.
- **Dashboard:** Billing page with plan status, usage meters, plan picker with monthly and yearly, Razorpay Checkout or Stripe redirect, change plan, undo a downgrade, seats, cancel and resume, billing details with GST state list, invoices and payments. A banner on every page when a trial is about to end, a payment failed or the plan has ended. Owners manage; admins read.

### Tests

- Backend: 257 tests, all passing (22 new). Eight billing flows run against fake Razorpay and Stripe APIs built from the documented shapes: Super Admin prices (tax, gateway creation, retiring, currency by country, gateway failure stores nothing), Razorpay checkout with seats, forged confirmation, charge webhook, redelivery, IGST invoice and numbering, mid-period seat add-on, cancel and no resume; webhook signatures (wrong, stale, wrong secret, unknown business); failed payment, grace expiry and recovery; Stripe checkout with the invoice arriving first, export invoice, upgrade, seats, downgrade that waits for period end, undo, sweeper applying it, cancel and resume, failed renewal and cancellation, subscribing again; plan must fit usage; permissions and another business's invoices; trial reminder and end. RLS tests cover the five new tables, and the app role is refused when it tries to rewrite invoices, payments, events, prices or invoice numbers. Unit tests cover GSTIN check digits, the financial year in Indian time, the GST split and Stripe form encoding.
- `ruff`, `mypy --strict`, `eslint`, `tsc`, `next build` and `alembic check`: clean.
- Browser walkthrough against the running stack with local stand-ins for both gateways (screenshots in the project files under `phase7/`): Super Admin adding a price, trial billing page, invalid GSTIN, Razorpay Checkout, active plan with the invoice, CGST and SGST invoice and its print view, seats blocking a downgrade, scheduled downgrade, cancel confirmation, dark mode, a phone at 390 px with no sideways scrolling, a US business on USD prices, Stripe Checkout, and the export invoice.

### Not verified here

- Real Razorpay and Stripe accounts in test mode. The clients follow the documented APIs and were exercised against fakes of them; the first real test purchase in each gateway is a go-live step (docs/BILLING.md, "Setting up for real").
- Razorpay Checkout's own script was replaced by a stand-in in the browser run (this environment can't reach it).

### Known limitations of Phase 7

- No refunds or credit notes in the app (use the gateway dashboard), no coupons.
- A Razorpay cancellation can't be undone (Razorpay has no resume for it); the business subscribes again after it ends.
- Changing currency means cancelling and subscribing again.
- The SAC code, GST rate, LUT and invoice prefix must be confirmed by XploSys's accountant before going live.

## Phase 6 report

### What is complete

- **Analytics API:** `GET /analytics/overview?days=7|30|90` (supervisors and above) returns totals with the previous period, messages per day, busiest weekday and hour, handover reasons, first-reply times (median and 90th percentile, assistant vs team), time to a person after a handover, a per-person team table, leads created and won with value, message delivery by status with top errors and billable categories, open unanswered questions, and AI requests and estimated cost (only for roles that can configure AI or see billing). Days and hours are in the business's time zone. `GET /analytics/setup` (every member) gives setup progress.
- **Overview page:** inbox tiles that open the matching inbox tab, the five newest conversations needing a person, a setup checklist that links each step only for people allowed to do it and disappears when done, the last-7-days figures for supervisors and above, and the plan card.
- **Analytics page:** period switch, four headline tiles with change against the previous period, a daily messages line chart with a hover and keyboard readout, a weekday-by-hour heatmap, handover reasons, first-reply times, message delivery and WhatsApp billable counts, AI usage, and the team table. Every chart has a table view. Agents see a plain explanation instead.
- **States:** loading, empty (no messages in the period, no handovers, nothing sent, no team replies) and error states on both pages; reloading a period keeps the previous figures dimmed instead of blanking the page.
- **Design system:** chart colours added as tokens in light and dark mode, checked with a palette validator for colour-blind separation and contrast. The inbox accepts `?view=` links.
- **Screens:** all 16 screens in the brief now exist on the shared design system: sign in and register, email verification, business setup at registration, overview, WhatsApp, AI assistant, AI providers (on the AI page), knowledge base, inbox, contacts, leads, analytics, plan (overview card; full billing screens are Phase 7), team, account, Super Admin.

### Tests

- Backend: 235 tests, all passing (4 new). They cover the period window in the business's time zone, every overview figure on a scripted day (AI reply, AI handover, takeover and reply, a won lead, a read and a failed message), previous-period comparison, permissions (agents refused, supervisors without AI cost), period validation, another business's data not being counted, and setup progress.
- `ruff`, `mypy --strict`, `eslint`, `tsc` and `next build`: clean.
- Browser walkthrough against the running stack (screenshots in the project files under `phase6/`): overview, analytics at 30, 7 and 90 days, chart hover, heatmap hover, table view, dark mode, the inbox opened from a tile link, a phone at 390 px with no sideways scrolling, and an agent's overview with no analytics access.

### Not verified here

- Query speed on large data. The queries use the existing time indexes; ADR-036 sets the point at which to add daily rollups.

### Known limitations of Phase 6

- No custom date range, export to CSV, or per-number filter on analytics yet.
- WhatsApp charges are counted, not priced (no rate card is stored; Meta bills businesses directly).
- Subscription management screens (change plan, invoices, payment) arrive with Phase 7; the overview shows the current plan.

## Phase 5 report

### What is complete

- **Data model (migration 0005):** `conversation_notes`, `human_handover_sessions` (one open session per conversation), `conversation_summaries`, `unanswered_questions`, `leads` and `lead_activities` (append-only for the app role). Conversations gain assignment with time, `awaiting_reply` and `closed_at`; contacts gain tags (GIN index). Trigram indexes for search. Forced RLS on all six new tables. The migration backfills existing data (handover sessions for conversations already handed over, waiting state, close times).
- **Inbox API:** tabs (open, mine, unassigned, needs a person, waiting for reply, closed) with counts; search by contact name, phone, username or message text; filters by tag, assignee and number; one conversation by ID.
- **Workflow:** assign and unassign with role rules, take over from the assistant and hand back (the old AI switch uses the same path), close and reopen (a customer message reopens), handover history, internal notes (author edits; author or supervisor deletes). Assignment, takeover, release, close and note deletion are audited. Removing a member returns their conversations, open takeovers and leads to the queue.
- **Assistant integration:** every AI handover opens a session, records the customer's question and the passages found, and (setting on by default) queues a summary. A summary can also be requested; it uses the assistant's model chain, is recorded as usage with purpose `summary`, and a lost job is re-sent by a sweeper.
- **Contacts API:** list with search, tag and consent filters; detail with conversations and leads; edit name, language and tags; tag list with counts.
- **Unanswered questions:** listed on the Knowledge page; owners and admins resolve, dismiss or reopen them.
- **Leads:** create from a conversation or a contact, status pipeline, value with currency (default from the business's country), owner (same rules as assignment), notes, full history, filters and search, pipeline summary; delete for supervisors and above.
- **Frontend:** the Conversations page has tabs with counts, search and tag filter, assignee and "Assigned to" info, take over, hand back, close, and a side panel with the AI summary, notes, contact details and tags, consent, leads and handover history. It works on phones (the list and the conversation take turns). New Contacts and Leads pages, an unanswered-questions card on the Knowledge page, and the summary setting on the AI assistant page.

### Tests

- Backend: 231 tests, all passing (22 new). Integration tests cover inbox views, counts and search (including escaped wildcards), awaiting reply and reopen, reactions, assignment rules for agents and supervisors, member removal, takeover and release with history, the AI handover recording the question and writing a summary (prompt shape and usage checked), summary failures and the setting, failed models handing over once, notes permissions, contacts and tags, the lead pipeline with history and permissions, cross-tenant access, and the summary sweeper. RLS tests cover the six new tables, including append-only lead history.
- `ruff`, `mypy --strict`, `eslint`, `tsc` and `next build`: clean. Migration up, down and up again ran clean on a fresh database and on the Phase 4 data, and `alembic check` finds no drift.
- Browser walkthrough against the running stack with fake Meta and AI APIs (screenshots in the project files under `phase5/`): an AI handover with its summary, take over, add a note, edit name and tags, create a lead, assign to an agent, reply and close, search by message text, move the lead to qualified with a note, filter contacts by tag, resolve the unanswered question, and the agent's view on a phone.

### Fixed along the way

- On phones the inbox was wider than the screen and the open conversation sat below the whole list; the columns no longer stretch to their content and the list and conversation now take turns.
- Removing a member left their name on an open takeover ("handled by"); it is now cleared with their assignments.

### Not verified here

- Summaries from real Gemini, OpenAI and DeepSeek models (fakes built from the documented API shapes). Check one with a real key before launch.
- Search speed at large volumes; the trigram indexes are in place but have only been tried on small data.

### Known limitations of Phase 5

- The inbox refreshes on a timer (15 seconds for the list, 5 for the open conversation); live updates over a socket are not built yet.
- No bulk actions (assign or close several at once), saved views or canned replies.
- Leads have no custom fields, stages per business or reminders; the pipeline stages are fixed.
- Contacts cannot be merged or imported, and there is no CSV export yet.

## Phase 4 report

### What is complete

- **Data model (migration 0004):** `knowledge_settings` (embedding model, a pending model during a switch, match score, passages per answer), `knowledge_documents`, `knowledge_document_versions`, `knowledge_chunks` and `knowledge_chunk_embeddings` (`vector(1536)`, HNSW cosine). `ai_models.kind` (chat or embedding). Forced RLS on all five tables; definer functions for the sweeper return only IDs.
- **Storage and scanning:** private S3-compatible bucket through the S3 API, presigned downloads forced to attachment, and ClamAV scanning of every file and fetched page before parsing (required outside development).
- **Upload checks:** body size capped before reading, content must match the extension, zip-bomb limits, macros and legacy Office files refused, plan limits on documents and storage.
- **Extraction:** PDF (per page), Word (heading paths and tables), Excel and CSV (each row as `Header: value` pairs), text, Markdown and web pages (navigation and scripts dropped). Clear messages for scanned or password-protected PDFs and oversized documents.
- **Web pages:** fetched with SSRF protection (public addresses only, pinned connection, manual redirects, type and size caps); "Fetch again" makes a new version.
- **Chunking and embeddings:** heading-aware chunks with overlap and page or sheet references; OpenAI and Gemini embeddings at 1536 dimensions, batched, retried, metered with cost.
- **Versions:** every upload, edit or fetch is a new version; answers use the active version until the new one is ready; identical uploads are refused; old versions can be downloaded or deleted; failed versions can be retried.
- **Re-indexing:** changing the embedding model re-embeds in batches beside the old vectors, then switches; answers keep working meanwhile; errors are shown with a retry.
- **Retrieval in answers:** automatic replies and the preview embed the latest message and recent context, search the business's passages above its threshold, give them to the model as reference excerpts, count them as grounding, and store the sources in the message provenance.
- **Frontend:** Knowledge base page (search settings and re-index progress, add a file, web page or note, document list with live status, versions with download, retry and delete, the passages the assistant reads, and a "Test a question" card with scores). Sources under AI replies in conversations and in the preview. Embedding models in the Super Admin catalogue.
- **Docker Compose:** SeaweedFS (bucket created by the migrate step) and ClamAV (optional `scan` profile in development).

### Tests

- Backend: 209 tests, all passing (54 new). Integration tests run against real PostgreSQL with pgvector, Redis and SeaweedFS, a fake clamd and fake providers: every format to chunks, presigned download, refused file types, malware rejected, scanner outage and retry, quotas, upload size limit, versions and notes, deletion removing files, tenant privacy, web pages with redirects and every SSRF case, answers grounded by a document with sources, a price outside the passages handed over, retrieval failure, preview, search test, embedding failure and retry, wrong vector size, model switch with errors and retry, the sweeper, and duplicate jobs. Unit tests cover chunking, file detection, extraction and address checks.
- `ruff`, `mypy --strict`, `eslint`, `tsc` and `next build`: clean. Migration up, down and up again ran clean, and `alembic check` finds no drift.
- Browser walkthrough against the running stack with fake providers: choose the embedding model, upload a Word file and a spreadsheet, add a web page and a note, see a 12 MB file refused with a reason, view passages, test a question, preview an answer with its source, and see "Sources" under an automatic WhatsApp reply.

### Fixed along the way

- The app's `/api` proxy cut request bodies at 10 MB (a Next.js default), which would have broken larger uploads; the limit now follows `KNOWLEDGE_MAX_FILE_MB`.
- Searching with only the last three customer messages joined let an earlier topic drown out a new question; the latest message is now searched on its own as well.

### Not verified here

- Real OpenAI and Gemini embedding calls (fakes built from the documented request and response shapes). Before launch, add a real key and run "Test a question".
- The real ClamAV image in the full stack (the protocol is tested against a fake clamd; the image needs 3 to 4 GB of memory).
- A production object store (Phase 9).

### Known limitations of Phase 4

- No OCR for scanned PDFs, and no images, `.doc`, `.xls` or `.pptx`.
- Web pages are fetched one at a time when asked; there is no site crawl or scheduled refresh yet.
- Embedding models must return 1536 dimensions.
- Retrieval scores depend on the model; the default threshold of 0.30 should be checked with the search test once a real model is chosen.

## Phase 3 report

### What is complete

- **Data model (migration 0003):** `ai_models` and `ai_model_prices` (platform catalogue, read-only for the app role), `ai_provider_configs` (per business and provider, key envelope-encrypted), `bot_configurations` and `bot_configuration_versions` (the assistant and its immutable settings versions), `usage_records` (append-only metering). `conversations.needs_human` and `messages.ai_provenance` added. RLS on every tenant table; the app role cannot rewrite metering, versions or the catalogue.
- **Provider adapters** for OpenAI (Responses API), Google Gemini (generateContent, key in a header) and DeepSeek (chat completions), each with structured JSON output, token usage, model listing and one error taxonomy with retryable errors.
- **Keys:** a business's own key per provider, or XploSys's key when the plan allows (`platform_ai_keys`). Keys are checked by listing models; only the last four characters are ever shown.
- **Catalogue:** model IDs and dated prices managed by the Super Admin (sync from provider, add by hand, activate, add a price). Cost estimates use the price in force when the call was made.
- **Orchestrator:** main model plus up to three fallbacks, retries on retryable errors, no database transaction held during provider calls.
- **Validator:** JSON shape, length, secrets and prompt leakage, and grounding of prices, percentages, times, phone numbers, emails and links against the business information and the customer's words. Failed answers try the next model; if none passes the handover message is sent.
- **Automatic replies:** debounced job per inbound message, per-conversation Redis lock, and checks for superseded, already answered, AI off, opted out, window closed and monthly quota. Handover turns AI off for the conversation and marks it "Needs a person". Replies go through the existing WhatsApp outbox with provenance.
- **Metering:** one `ai_call` record per provider request (tokens, estimated cost, key source, latency, outcome) and one `ai_reply` per AI message sent, counted against `monthly_ai_messages`.
- **Frontend:** AI assistant page (providers and keys, assistant settings with model and fallbacks, preview chat using the unsaved settings, version history with rollback, monthly usage and estimated cost, automatic-reply switch with warnings when something is missing); conversations show "Needs a person", an AI on/off control per conversation and the model on AI messages; the Super Admin page has the model catalogue.

### Tests

- Backend: 155 tests, all passing. 16 new integration tests run against a fake that answers each provider's documented request and response shapes: keys and the Gemini header, platform keys and the plan, permissions, versions, a grounded reply with its cost, fallback, blocked and cut-off answers, an ungrounded price leading to handover, `needs_human`, invalid JSON and prompt leakage, message bursts, duplicates, every no-reply case including the quota, the preview and the admin catalogue. Isolation tests cover the new tables.
- `ruff`, `mypy --strict`, `eslint`, `tsc` and `next build`: clean. Migration up, down and up again ran clean, and `alembic check` finds no drift.
- Browser walkthrough against the running stack with fake providers and a fake Meta: turn on Gemini with XploSys's key and check it, save assistant settings, preview a grounded answer and an ungrounded one (handed over), turn on automatic replies, receive a WhatsApp message and see the AI reply sent with its model, receive a complaint and see the handover and "Needs a person", resume AI and see it answer again, and see usage with the estimated cost.

### Not verified here

- Real provider accounts. All calls ran against fakes built from the official API documentation. Before launch, run "Check key" and a preview with a real key for each provider.
- The Super Admin catalogue card in a browser (the API is covered by tests; the console needs a Super Admin with two-factor sign-in).

### Known limitations of Phase 3

- The assistant only knows what is typed into its settings until knowledge documents arrive in Phase 4.
- Images, audio and video are not interpreted.
- Handed-over conversations show a badge; assignment and a live inbox come in Phase 5.
- Model temperature is left at each provider's default.

## Phase 2 report

### What is complete

- **Data model (migration 0002):** `whatsapp_accounts`, `whatsapp_numbers`, `whatsapp_credentials` (envelope-encrypted, AAD bound to tenant, account and kind), `message_templates`, `contacts`, `conversations`, `messages`, `webhook_events` (inbox) and `webhook_outbox`. RLS is enabled and forced on all of them, with composite tenant foreign keys. The app role cannot delete webhook events or credentials. WABA IDs and phone number IDs are unique across the platform, so one WhatsApp account can only belong to one business.
- **Webhook receiver** at `/api/v1/webhooks/whatsapp`: GET verification with the verify token, POST with `X-Hub-Signature-256` checked over the raw body in constant time, 3 MB body limit. Each change is stored before we answer 200 (persist-then-ack), deduplicated by a hash of its content, routed to the tenant by phone number ID (falling back to WABA ID) through narrow SECURITY DEFINER lookups, and processed by a worker. Unknown numbers are kept as `unroutable` for support.
- **Processing:** inbound messages of every type (text, media, location, contacts, buttons, lists, reactions, unsupported), statuses with monotonic ordering (a late `delivered` never overwrites `read`; `failed` is final; a status that arrives before its message is retried), template status and category changes, quality updates, account updates (app uninstalled, account deleted, policy violations) and number or user-ID changes.
- **Business-scoped user IDs (BSUID):** Meta now sends `user_id` and may omit the phone number for users with a username. Contacts are matched on BSUID first and then phone. Replies go to `recipient` when no phone is known; authentication templates are blocked without a phone, as Meta requires.
- **Sending:** a transactional outbox. The API stores the message and job in one transaction and dispatches after commit; a beat sweeper re-dispatches anything missed. Sending is at most once: a job is marked `sending` before the Meta call, and a job stuck in `sending` is marked `unknown` and its message failed with `send_outcome_unknown` rather than risking a duplicate. Rate limits and server errors retry with backoff; a token error moves the account to `needs_reconnect`.
- **Policy engine v1:** blocks sending when the account needs reconnecting or the number is not connected, when the contact opted out, outside the 24-hour window unless it is an approved template in the right language, marketing templates without opt-in or on a RED-quality number, and authentication templates without a phone number. STOP / START keywords (configurable) update consent.
- **Embedded Signup:** the browser runs Meta's popup with the Facebook JS SDK; the backend exchanges the code at once and checks everything itself (`debug_token` validity, app ID, scopes, granular WABA targets, the WABA and number really belong to that token, and neither is connected to another business). It then subscribes our app, registers the number with a generated 6-digit PIN (stored encrypted, viewable by admins with an audit entry), or marks it as a WhatsApp Business app number when coexistence is used. A one-time onboarding session in Redis ties the code to the signed-in user and business. The plan's number limit is enforced.
- **Account health:** daily and on-demand checks of token validity, webhook subscription (re-subscribes if missing), number status, quality and messaging limit, and numbers removed from the WABA. Reconnect runs Embedded Signup again for the same WABA. Disconnect unsubscribes the app and revokes stored credentials. Template sync is paginated and marks templates deleted at Meta as `DELETED`.
- **Frontend:** a WhatsApp page (connect, reconnect, health badges and issues, numbers with quality, limit and status, register a number, show PIN, health check, template sync, disconnect, send a message) and a Conversations page (conversation list with unread counts and reply-window state, message thread with delivery status and failure reasons, text replies inside the window, template replies with placeholder values outside it, consent controls). Missing Meta settings are listed instead of showing a button that cannot work.

### Tests

- Backend: 113 tests, all passing, against real PostgreSQL + pgvector and Redis. About 30 integration tests run the whole flow against a fake Graph API that answers Meta's documented endpoints: onboarding success and every rejection, webhook verification and signatures, dedup, BSUID-only contacts, STOP and START, sending in and out of the window, statuses in any order, retries, token revocation and reconnect, stale jobs, the sweeper, health checks, account updates, disconnect, the plan limit and cross-tenant access. Isolation tests cover every new table. A worker test checks that a fresh worker process can resolve every model.
- `ruff`, `mypy --strict`, `eslint`, `tsc` and `next build`: clean.
- Browser walkthrough against the running stack with a fake Meta: register, connect WhatsApp through the Embedded Signup flow, sync templates, receive a signed inbound webhook, see the conversation, reply, watch the status change to Delivered, record opt-in, and send a template. This run found and fixed a bug where the worker had not loaded every model and could not save inbound messages.

### Not verified here

- Real Meta. Everything above ran against a fake Graph API built from Meta's documentation. The first real test needs the Meta app set up (see docs/META_INTEGRATION.md §7) and someone with a role on the app, using Meta's test number.

### Known limitations of Phase 2

- Inbound media IDs are stored but files are not downloaded yet (object storage arrives with Phase 5).
- Meta's pricing category is recorded per message; cost estimates come with billing in Phase 7.
- The dashboard refreshes conversations every few seconds; live updates over a socket come in Phase 5.
- Template creation and editing from the dashboard is Phase 5/6; templates are created in WhatsApp Manager for now.

## Phase 1 report

### What is complete

- **Backend foundation:** FastAPI app factory, typed settings (fail fast on missing secrets outside development), structured JSON logging with secret redaction, request IDs, uniform error envelope, security headers, `/health/live` and `/health/ready`.
- **Database:** four PostgreSQL roles (`xplo_owner` migrations, `xplo_app` RLS-enforced, `xplo_admin` BYPASSRLS for Super Admin only, `xplo_definer` NOLOGIN for two narrow SECURITY DEFINER lookups), extensions `vector`, `citext`, `pg_trgm`, UUIDv7 keys, UTC timestamps.
- **Migration 0001:** `users`, `user_action_tokens`, `refresh_tokens`, `tenants`, `tenant_memberships`, `tenant_invitations`, `audit_logs`, `subscription_plans`, `subscriptions`, with RLS `ENABLE` + `FORCE` and fail-closed tenant policies, append-only audit log, read-only plan catalogue for the app role.
- **Authentication:** register (creates business, owner membership and 14-day trial), email verification, login with lockout, ES256 access tokens (15 min), rotating refresh tokens in an HttpOnly SameSite=Strict cookie with reuse detection that revokes the whole session family, logout, switch business, password reset and change (both sign out other sessions), TOTP two-factor with single-use recovery codes, MFA mandatory for Super Admin.
- **Tenants and RBAC:** tenant profile, members (role change, suspend, remove, last-owner protection, role hierarchy), invitations (seat limit from plan, re-invite replaces, revoke, accept for new or existing users), permission matrix for all five roles including permissions for later phases, audit log API.
- **Super Admin API:** list and search businesses, suspend and reactivate, all audited.
- **Workers:** Celery app with queues for later phases, a per-process async runtime, SMTP email task with retries, token clean-up on a beat schedule.
- **Frontend (Next.js 16):** sign in (with two-factor step), register, verify email, forgot and reset password, accept invitation, overview, team, business settings, account and security (two-factor setup with QR code, password change), Super Admin. Theme matched to xplosys.com: Plus Jakarta Sans, the indigo, violet and cyan gradient, pill buttons, 18 px cards, the dark hero band on sign-in screens, and a light/dark switch.
- **Infrastructure:** Dockerfiles (non-root, health checks), Docker Compose for local development (PostgreSQL, Redis with persistence, one-shot migrate, API, worker, beat, frontend, Mailpit), GitHub Actions CI (lint, type check, tests, build, image build).

### Tests

- Backend: 56 tests, all passing (`uv run pytest -q`), against real PostgreSQL 17 + pgvector and Redis via Testcontainers. They cover security primitives, permission matrix, log redaction, every auth flow, tenant and member management, Super Admin, workers, migration round trip plus a models-vs-migration check, and **tenant isolation**: RLS enabled and forced on every tenant table, app role cannot bypass RLS, no cross-tenant read, update, delete, insert or move, no rows without tenant context, context does not leak across pooled transactions, and API calls cannot reach another tenant's records by ID. A new tenant table without RLS or without an isolation case fails the suite.
- `ruff check`, `ruff format --check`, `mypy --strict` (app): clean. Frontend `eslint`, `tsc`, `next build`: clean.
- Browser walkthrough (Playwright, against the running stack): register, dashboard, verification email delivered via Celery and SMTP to Mailpit, verify link, invite a teammate, save business settings, start two-factor setup, dark mode, reload keeps the session, sign out.

### Not verified here

- The Docker images were not built inside the development sandbox because its network blocks package downloads from inside `docker build`. The CI `docker` job builds both images on every push. PostgreSQL, Redis and Mailpit ran from the Compose file; the API, worker and frontend ran on the host against them.

## Open decisions (need the owner's input)

1. Approve Phase 9 and start Phase 10 (production testing and release).
2. Production object storage: a managed bucket or self-hosted SeaweedFS (docs/DEPLOYMENT_UBUNTU.md §4), and an off-site backup provider in another company or region.
3. Billing entity for UK/US customers (affects Stripe), and the GST details for invoices: SAC code, LUT number, invoice prefix (docs/BILLING.md).
4. Legal review of Meta's WhatsApp Business Solution Terms for AI use, of DPDP/GDPR obligations, and of the breach notification steps in docs/SECURITY.md.

## Actions for XploSys outside the code (start now)

- Create or confirm the Meta Business Portfolio and start business verification.
- Create the Meta Developer App (Business type) and add WhatsApp.
- Publish privacy policy, terms and data deletion pages on xplowhatsapp.com.
- Rent the production server (4 vCPU, 8 GB), point `app.` (and `files.`) DNS at it, create a Sentry organisation, and follow docs/DEPLOYMENT_UBUNTU.md.
- Open Gemini, OpenAI and DeepSeek API accounts, put the platform keys in the server environment, and enter current model prices in the Super Admin catalogue (see docs/AI_PROVIDERS.md).

## Known limitations (deliberate, documented)

- Inbound media is stored and shown, but AI does not interpret images/audio/video initially.
- Scanned PDFs need OCR, planned as an optional later feature.
- US healthcare PHI is not supported (no WhatsApp HIPAA BAA).
- WhatsApp messaging charges are paid by each business to Meta directly (Tech Provider model); XploWhatsApp tracks and estimates them.
- Membership and role are read from the database on every request (no cache yet). A short Redis cache can be added if this shows up in latency metrics.
