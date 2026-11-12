# XploWhatsApp

AI-powered WhatsApp customer engagement and automation platform by XploSys. Multi-tenant SaaS built on the official Meta WhatsApp Cloud API.

**Status:** Phase 1 (foundation) complete. See [PROJECT_STATUS.md](PROJECT_STATUS.md).

- [ARCHITECTURE.md](ARCHITECTURE.md): system design, diagrams, data model, security decisions
- [docs/META_INTEGRATION.md](docs/META_INTEGRATION.md): Meta setup, approvals and onboarding sequence
- [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md): phased plan

Stack: Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Celery, PostgreSQL 17 + pgvector, Redis, Next.js 16 + TypeScript + Tailwind CSS 4, Docker Compose.

## Run locally with Docker

```bash
cp .env.example .env
# Put long random values in the *_PASSWORD lines, then generate signing and encryption keys:
docker compose run --rm --no-deps api python -m app.cli generate-secrets   # paste the output into .env
docker compose up -d --build
```

| Service | URL |
|---|---|
| App | http://localhost:3000 |
| API docs (development only) | http://localhost:8000/docs |
| Mailpit (verification and invite emails) | http://localhost:8025 |

The one-shot `migrate` service creates the database roles, runs migrations and seeds the plan catalogue on every start (idempotent).

To connect WhatsApp, fill in the `META_*` lines in `.env` (see docs/META_INTEGRATION.md §7 for where each value comes from and how to set up the webhook). Without them the WhatsApp page lists what is missing.

Create a Super Admin (they must turn on two-factor authentication before using the admin console):

```bash
docker compose run --rm api python -m app.cli create-super-admin you@xplosys.com
```

## Develop without Docker for the app processes

```bash
# Backend (needs PostgreSQL with pgvector and Redis; see .env.example for URLs)
cd backend && uv sync
uv run python -m app.cli bootstrap-db && uv run alembic upgrade head && uv run python -m app.cli seed
uv run uvicorn app.main:app --reload
uv run celery -A app.workers.celery_app worker -Q default,messages,knowledge

# Frontend (proxies /api to http://localhost:8000)
cd frontend && pnpm install && pnpm dev
```

## Tests and checks

```bash
cd backend && uv run pytest -q          # needs Docker: Testcontainers starts PostgreSQL and Redis
cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app
cd frontend && pnpm lint && pnpm typecheck && pnpm build
```

## Layout

```
backend/   FastAPI app (app/), Alembic migrations, Celery workers, tests
frontend/  Next.js dashboard (App Router)
docs/      Architecture, Meta integration, plan
```
