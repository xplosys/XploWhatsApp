# End-to-end and load tests

The whole product runs for real (API, workers, web app, Postgres, Redis, object storage)
and a real browser drives it. Only the far side of two kinds of HTTP calls is simulated:

- **Meta's Graph API** (Embedded Signup token exchange, account and number lookups,
  webhook subscription, registration, sends), and
- **the AI providers** (OpenAI, Gemini and DeepSeek: model lists, chat and embeddings).

Both are served by `fakes/server.py`, which reuses the backend's integration-test fakes
(`backend/tests/support`) over HTTP. The API and workers reach it through
`META_GRAPH_BASE_URL` and the `*_BASE_URL` settings. Facebook's JavaScript SDK is replaced
in the browser by a stub that finishes Embedded Signup the way Meta's popup does. Customer
messages arrive as signed Meta webhooks, sent by the fake service to the real webhook URL.

Never point a production deployment at the fake service.

## What is tested

| Test | Journey |
|---|---|
| `tests/journey.spec.ts` | A clinic registers, connects WhatsApp, picks Gemini, uploads a PDF price list, turns on automatic replies. A customer asks a price; the assistant answers from the PDF (the AI call carries the passage, the reply shows its source), the reply leaves through the WhatsApp API, usage records the model, and the overview counts the conversation |
| `tests/handover.spec.ts` | The assistant hands a complaint to the team; the owner replies from the inbox (sent through the WhatsApp API) and hands the conversation back; the assistant answers the next message |

Before the tests, `tests/platform-setup.ts` does what XploSys does once: it creates a Super
Admin with the CLI, signs in with two-factor, syncs Gemini's models, prices the chat model
and switches the models on.

## Run them with Docker (as CI does)

From the repository root, on a machine without a `.env` you care about:

```bash
e2e/scripts/test-env.sh        # writes .env with random passwords and fresh keys
docker compose -f docker-compose.yml -f e2e/docker-compose.e2e.yml up -d --build
cd e2e
pnpm install
pnpm exec playwright install --with-deps chromium
pnpm test                      # pnpm report opens the HTML report afterwards
```

`test-env.sh` refuses to run if `.env` exists. `docker compose ... down -v` removes the
stack and its data.

The `e2e` job in `.github/workflows/ci.yml` runs exactly this on every push. On failure it
prints each failed test's page snapshot and the service logs, and uploads screenshots,
traces and the HTML report as the `e2e-report` artifact.

## Run them against processes you started yourself

Start the API, both workers, the web app and the fake service with the same settings as
`docker-compose.e2e.yml` (the `x-e2e-env` block), then point the tests at them:

| Variable | Default | Meaning |
|---|---|---|
| `E2E_WEB_URL` | `http://127.0.0.1:3000` | The web app |
| `E2E_API_URL` | `http://127.0.0.1:8000` | The API |
| `E2E_FAKES_URL` | `http://127.0.0.1:9000` | The fake Meta and AI service |
| `E2E_ADMIN_CLI` | `docker compose ... exec -T api python -m app.cli` | How to run the backend CLI (Super Admin creation) |

For example, with the backend in a local virtual environment:

```bash
E2E_ADMIN_CLI="bash -c 'cd backend && .venv/bin/python -m app.cli \"\$@\"' --" pnpm test
```

Run the fake service with `PYTHONPATH=backend uvicorn --app-dir e2e/fakes server:app --port 9000`.
It reads:

| Variable | Meaning |
|---|---|
| `META_APP_ID`, `META_APP_SECRET` | Must match the API's, so webhook signatures verify |
| `E2E_WEBHOOK_URL` | Where to deliver customer messages (default `http://api:8000/api/v1/webhooks/whatsapp`) |
| `E2E_DEFAULT_AI_ANSWER` | Answer chat calls with this when no answer is queued (load tests). Unset: unexpected AI calls fail |
| `E2E_AI_LATENCY_MS` | How long each chat answer takes, like a real model (default 0) |

## Load tests

`load/whatsapp.js` is a [k6](https://grafana.com/docs/k6/) script with two scenarios at
the same time:

- **inbound:** new customers write at `RATE` messages a second. Each must get an AI reply
  sent through the WhatsApp API.
- **dashboard:** the team's screens poll inbox counts, the conversation list, the overview
  and the signed-in user at `DASHBOARD_RATE` requests a second.

When the traffic stops it waits up to `DRAIN_SECONDS` for every customer to be answered.
k6 exits non-zero if any target in docs/LAUNCH_CHECKLIST.md section 7 is missed: webhook
answered p95 under 500 ms and p99 under 1 s, no webhook rejected, each dashboard read p95
under 800 ms, under 1% dashboard errors, every customer answered, and reply p95 under
`REPLY_P95_SECONDS`.

1. Start the stack (either way above) with `E2E_DEFAULT_AI_ANSWER` set on the fake
   service, and `E2E_AI_LATENCY_MS` set to your chat model's real reply time.
2. Prepare a business. This runs platform set-up, raises the free trial's AI allowance on
   this throwaway stack (500 a month otherwise, after which conversations go to the team by
   design), then connects a business with automatic replies on through the UI. It writes
   the owner's token to `load/.target.json`, valid for the access token's lifetime.

   ```bash
   cd e2e && pnpm exec playwright test -c playwright.load.config.ts
   ```
3. Run k6 (installed, or with Docker):

   ```bash
   docker run --rm --network host --user "$(id -u)" -v "$PWD/load:/load" -w /load \
     grafana/k6:1.3.0 run -e RATE=5 -e DURATION=1m whatsapp.js
   ```

| Variable | Default | Meaning |
|---|---|---|
| `RATE` | 20 | Customer messages a second |
| `DASHBOARD_RATE` | 10 | Dashboard reads a second |
| `DURATION` | `2m` | How long to send |
| `DRAIN_SECONDS` | 180 | How long to wait for the last replies |
| `REPLY_P95_SECONDS` | 10 | Reply time target (add the model's latency) |
| `API_URL`, `FAKES_URL` | `http://127.0.0.1:8000`, `:9000` | Where the API and the fake service are |

The fake service measures each reply from the customer's message to the moment the reply
reaches the WhatsApp API, and reports it at `GET /_control/stats` (the script prints it).

Run load tests right after the stack has settled, not while the API is still starting: the
first requests after a restart are slower. Results from our runs, and how to size workers
from them, are in docs/LAUNCH_CHECKLIST.md section 7.
