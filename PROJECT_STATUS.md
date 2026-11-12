# XploWhatsApp — project status

**Last updated:** 2026-11-12
**Current phase:** Phase 1 complete, awaiting approval to start Phase 2 (Meta WhatsApp integration)

## Phase tracker

| Phase | Status | Notes |
|---|---|---|
| 0. Architecture and planning | Done (approved 2026-11-12) | ARCHITECTURE.md, docs/META_INTEGRATION.md, docs/IMPLEMENTATION_PLAN.md |
| 1. Foundation | Done, awaiting review | See below |
| 2. Meta WhatsApp integration | Not started | Start Meta business verification and Tech Provider process now |
| 3. AI provider orchestration | Not started | |
| 4. RAG knowledge engine | Not started | |
| 5. Conversation management | Not started | |
| 6. Customer dashboard | Not started | |
| 7. Billing and subscriptions | Not started | |
| 8. Security hardening | Not started | |
| 9. Monitoring and deployment | Not started | |
| 10. Production testing and release | Not started | |

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

1. Approve Phase 1 and start Phase 2.
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
