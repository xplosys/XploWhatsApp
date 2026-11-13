# XploWhatsApp

AI-powered WhatsApp customer engagement and automation platform by XploSys. Multi-tenant SaaS built on the official Meta WhatsApp Cloud API.

**Status:** Phases 1 to 4 (foundation, WhatsApp, AI assistant, knowledge base) complete. See [PROJECT_STATUS.md](PROJECT_STATUS.md).

- [ARCHITECTURE.md](ARCHITECTURE.md): system design, diagrams, data model, security decisions
- [docs/META_INTEGRATION.md](docs/META_INTEGRATION.md): Meta setup, approvals and onboarding sequence
- [docs/AI_PROVIDERS.md](docs/AI_PROVIDERS.md): AI providers, keys, model catalogue and how replies are checked
- [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md): knowledge base uploads, scanning, embeddings, retrieval and settings
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
| SeaweedFS S3 gateway (knowledge files; download links point here) | http://localhost:8333 |

The one-shot `migrate` service creates the database roles, runs migrations, seeds the plan catalogue and creates the private file bucket on every start (idempotent).

For the knowledge base, set `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY` in `.env` (any long random values in development). To scan uploads for viruses locally, set `CLAMAV_HOST=clamav` and start with `docker compose --profile scan up -d` (ClamAV needs about 3 to 4 GB of memory). As Super Admin, activate an embedding model so businesses can choose it (docs/KNOWLEDGE.md).

To connect WhatsApp, fill in the `META_*` lines in `.env` (see docs/META_INTEGRATION.md §7 for where each value comes from and how to set up the webhook). Without them the WhatsApp page lists what is missing.

For the AI assistant, optionally put XploSys's own provider keys in `OPENAI_API_KEY`, `GEMINI_API_KEY` and `DEEPSEEK_API_KEY` (businesses can also use their own keys). Then, as Super Admin, sync models, add prices and activate the models businesses may use (docs/AI_PROVIDERS.md §2).

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
