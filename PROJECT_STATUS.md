# XploWhatsApp — project status

**Last updated:** 2026-11-12
**Current phase:** Phase 2 complete, awaiting approval to start Phase 3 (AI provider orchestration)

## Phase tracker

| Phase | Status | Notes |
|---|---|---|
| 0. Architecture and planning | Done (approved 2026-11-12) | ARCHITECTURE.md, docs/META_INTEGRATION.md, docs/IMPLEMENTATION_PLAN.md |
| 1. Foundation | Done (PR #1) | See Phase 1 report |
| 2. Meta WhatsApp integration | Done, awaiting review | See Phase 2 report. Going live needs Meta App Review and Tech Provider approval |
| 3. AI provider orchestration | Not started | |
| 4. RAG knowledge engine | Not started | |
| 5. Conversation management | Not started | |
| 6. Customer dashboard | Not started | |
| 7. Billing and subscriptions | Not started | |
| 8. Security hardening | Not started | |
| 9. Monitoring and deployment | Not started | |
| 10. Production testing and release | Not started | |

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

1. Approve Phase 2 and start Phase 3.
2. Production object storage provider (decided by Phase 9).
3. Billing entity for UK/US customers (affects Stripe).
4. Legal review of Meta's WhatsApp Business Solution Terms for AI use, and of DPDP/GDPR obligations.

## Actions for XploSys outside the code (start now)

- Create or confirm the Meta Business Portfolio and start business verification.
- Create the Meta Developer App (Business type) and add WhatsApp.
- Publish privacy policy, terms and data deletion pages on xplowhatsapp.com.
- Open Gemini, OpenAI and DeepSeek API accounts for development.

## Known limitations (deliberate, documented)

- Inbound media is stored and shown, but AI does not interpret images/audio/video initially.
- Scanned PDFs need OCR, planned as an optional later feature.
- US healthcare PHI is not supported (no WhatsApp HIPAA BAA).
- WhatsApp messaging charges are paid by each business to Meta directly (Tech Provider model); XploWhatsApp tracks and estimates them.
- Membership and role are read from the database on every request (no cache yet). A short Redis cache can be added if this shows up in latency metrics.
