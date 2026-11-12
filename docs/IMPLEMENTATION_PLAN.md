# XploWhatsApp — phased implementation plan

Each phase ends with: list of files, decisions explained, install/config instructions, tests run with results, what is complete, what remains, and **your approval before the next phase starts**.

## Phase overview

| Phase | Scope | Main outputs | External dependency |
|---|---|---|---|
| **1. Foundation** | Repo, FastAPI, Next.js shell, Postgres+pgvector, Redis, Docker Compose, core models, Alembic, auth, tenants, RBAC, RLS and isolation tests | Running stack locally, auth and tenant APIs, CI | None |
| **2. Meta WhatsApp** | Webhook (GET/POST, signature, persistence, dedup), Cloud API client, outbound sending via outbox, status tracking, Embedded Signup (frontend + backend), account health, reconnect, templates sync, policy engine v1 | Messages in/out on Meta's test number, ESU working for app-role users | Meta app, test WABA. Start Tech Provider process |
| **3. AI orchestration** | Provider interface, Gemini/OpenAI/DeepSeek adapters, registry, catalogue with versioned prices, orchestrator with fallback, validator, usage service, assistant configuration + versions, `/ai/*` endpoints | Auto-replies on the test number | Provider API keys |
| **4. RAG** | Upload, ClamAV, extractors (PDF, DOCX, TXT, XLSX, CSV, URL), chunking, embeddings, pgvector retrieval, thresholds, versioning, re-indexing, source references | Grounded answers from tenant documents | None |
| **5. Conversations** | Contacts, inbox APIs, assignment, handover, notes, tags, search, summaries, unanswered tracking, leads | Full agent workflow via API | None |
| **6. Dashboard** | All 16 screens on the design system, real states, typed API client | Usable product UI | None |
| **7. Billing** | Plans, trials, subscriptions, upgrades/downgrades, seats, usage metering, quotas, invoices (GST), Razorpay, Stripe | Paid plans | Razorpay/Stripe accounts |
| **8. Security hardening** | MFA, key rotation, rate limits, audit coverage, upload hardening, prompt-injection tests, pen-test checklist, DPDP/GDPR data tools | `docs/SECURITY.md` | None |
| **9. Monitoring and deployment** | Prometheus metrics, Grafana dashboards, Sentry, alerts, prod compose, Nginx/TLS, backups/restore, CI/CD deploy, Ubuntu guide | `docs/DEPLOYMENT_UBUNTU.md` | VPS, domain, Sentry |
| **10. Production testing and release** | Full E2E (register → connect → upload PDF → Gemini → message → reply → usage → dashboard), load tests, launch checklist, App Review submission support | `docs/LAUNCH_CHECKLIST.md` | Meta approval |

MFA and audit logging foundations land in Phase 1 (tables and hooks); full enforcement and coverage are Phase 8.

---

## Phase 1: Foundation — implementation steps

### 1.1 Repository and tooling
1. Create the folder layout from ARCHITECTURE.md §6, `README.md`, `.gitignore`, `.editorconfig`, `.env.example`, `Makefile`.
2. Backend: `pyproject.toml` managed by `uv`, Python 3.12, with FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2 (async), asyncpg, Alembic, httpx, redis, celery, argon2-cffi, PyJWT[crypto], cryptography, structlog, pgvector, uuid-utils (UUIDv7). Dev: pytest, pytest-asyncio, testcontainers, ruff, mypy.
3. Frontend: Next.js (App Router) + TypeScript + Tailwind via `pnpm`, ESLint, Prettier, base layout and design tokens, login/register pages wired to the API.
4. Git init with a first commit (pushing to GitHub once you create the repository).

### 1.2 Core backend
5. `core/config.py`: typed settings, fails fast on missing required secrets.
6. `core/logging.py`: structlog JSON, request ID middleware, redaction processor.
7. `core/errors.py`: error envelope (`{"error": {"code", "message", "details", "request_id"}}`), exception handlers.
8. `main.py` app factory: CORS allow-list, routers, `/health/live` and `/health/ready` (checks DB and Redis).
9. `db/`: async engine, session dependency, declarative base with UUIDv7 PK and UTC timestamps mixin, `TenantScopedMixin`, transaction-local tenant context helper.

### 1.3 Database and migrations
10. `infra/postgres/init/`: create roles `xplo_owner`, `xplo_app` (NOBYPASSRLS), `xplo_admin`; extensions `vector`, `citext`, `pg_trgm`.
11. Alembic configured for async, running as `xplo_owner`.
12. Migration 0001: `tenants`, `users`, `tenant_memberships`, `tenant_invitations`, `refresh_tokens`, `audit_logs`, `subscription_plans`, `subscriptions` (minimal, for trial and limits), plus RLS policies, `FORCE ROW LEVEL SECURITY`, grants to `xplo_app`.
13. Seed script: default plans (Free Trial etc., limits as data) and a Super Admin bootstrap command (prompted password, MFA set up on first login).

Other tables (WhatsApp, AI, knowledge, conversations, billing details) arrive with their phases, each with its own migration and isolation tests. The ER diagram already accounts for them.

### 1.4 Authentication
14. `POST /auth/register`: creates user + tenant + owner membership + trial subscription in one transaction; sends verification email (SMTP in prod, console/Mailpit in dev).
15. `POST /auth/verify-email`, `POST /auth/resend-verification`.
16. `POST /auth/login`: Argon2id verify, lockout/backoff, issues ES256 access JWT (15 min) and rotating refresh cookie.
17. `POST /auth/refresh`: rotation with family reuse detection.
18. `POST /auth/logout`: revokes the refresh family for this session.
19. `POST /auth/switch-tenant`: membership check, new token pair.
20. MFA (TOTP) enrolment/verify endpoints, required for Super Admin.
21. Password reset request/confirm.

### 1.5 Tenants and RBAC
22. `GET /tenants/current`, `PUT /tenants/current` (Owner/Admin).
23. Members: list, change role, remove; invitations: create, list, revoke, accept (token hashed, expiring).
24. Permission matrix and `require_permission(...)` dependency; Super Admin endpoints use the separate admin engine.
25. Audit log entries for auth and membership events.

### 1.6 Infrastructure
26. `docker-compose.yml`: `postgres` (pgvector image, volume), `redis` (AOF persistence), `api`, `worker` (Celery, ping task only in Phase 1), `beat`, `frontend`, `mailpit` (dev only). Health checks, restart policies, internal network; only api/frontend ports exposed locally.
27. Backend and frontend Dockerfiles (multi-stage, non-root user).
28. `.github/workflows/ci.yml`: ruff, mypy, pytest with testcontainers, frontend lint/type-check/build.

### 1.7 Tests (must pass before Phase 1 is reported complete)
29. Unit: password hashing, JWT issue/verify, refresh rotation and reuse detection, permission matrix.
30. API integration: register, verify, login, refresh, logout, switch tenant, tenant update permissions, invitations.
31. **Tenant isolation:** generated test over every RLS table proving tenant A cannot read, write, update or delete tenant B's rows, the app role cannot bypass RLS, and a missing tenant context returns zero rows.
32. Migration test: upgrade from empty to head and downgrade back.

### Phase 1 exit criteria
- `docker compose up` starts the stack and `/health/ready` is green.
- A user can register, verify, log in and manage the team from the UI.
- All tests pass in CI, including isolation tests.
- PROJECT_STATUS.md updated with results.
