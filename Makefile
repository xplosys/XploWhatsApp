.PHONY: up down logs test test-backend lint-backend frontend-check secrets super-admin

up:            ## Build and start the local stack
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api worker

test: test-backend frontend-check

test-backend:  ## Backend tests (needs Docker for Testcontainers)
	cd backend && uv run pytest -q

lint-backend:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app

frontend-check:
	cd frontend && pnpm lint && pnpm typecheck && pnpm build

secrets:       ## Print fresh JWT and encryption keys for .env
	cd backend && uv run python -m app.cli generate-secrets

super-admin:   ## make super-admin EMAIL=you@xplosys.com
	docker compose run --rm api python -m app.cli create-super-admin $(EMAIL)
