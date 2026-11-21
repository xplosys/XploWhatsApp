/**
 * What XploSys does once before any business signs up, done the way an operator would:
 * create a Super Admin with the CLI, sign in with two-factor, sync Gemini's models from
 * the provider, price the chat model, and switch the chat and embedding models on.
 */
import { execSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { API_URL, CSRF, FAKES_URL, json, totp } from "./support";

// How to run the backend CLI. CI and a local Docker stack use the default.
const CLI =
  process.env.E2E_ADMIN_CLI ??
  "docker compose -f docker-compose.yml -f e2e/docker-compose.e2e.yml exec -T api python -m app.cli";
const REPO_ROOT = `${__dirname}/../..`;
export const CHAT_MODEL = "gemini-test-flash";
export const EMBEDDING_MODEL = "gemini-embedding-test";

async function waitFor(url: string, what: string) {
  const until = Date.now() + 300_000;
  for (;;) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      /* not up yet */
    }
    if (Date.now() > until) throw new Error(`${what} did not answer at ${url}`);
    await new Promise((r) => setTimeout(r, 1000));
  }
}

async function post(path: string, body: unknown, headers: Record<string, string> = {}) {
  return fetch(`${API_URL}/api/v1${path}`, {
    method: "POST",
    headers: { ...CSRF, "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
}

export default async function platformSetup() {
  await waitFor(`${API_URL}/health/ready`, "The API");
  await waitFor(`${process.env.E2E_WEB_URL ?? "http://127.0.0.1:3000"}/login`, "The web app");
  await waitFor(`${FAKES_URL}/_control/health`, "The fake Meta and AI service");

  const email = `admin-${Date.now()}@e2e.example.com`;
  const password = `E2e-Admin-${Date.now()}-Pass!`;
  execSync(`${CLI} create-super-admin ${email}`, {
    cwd: REPO_ROOT,
    input: `${password}\n${password}\n`,
    stdio: ["pipe", "inherit", "inherit"],
  });

  const login = await json(await post("/auth/login", { email, password }), "admin sign-in");
  const bearer = { Authorization: `Bearer ${login.access_token}` };
  const { secret } = await json(await post("/auth/mfa/setup", {}, bearer), "start two-factor");
  const enabled = await post("/auth/mfa/enable", { code: totp(secret) }, bearer);
  await json(enabled, "turn on two-factor");
  // Turning two-factor on moves the sign-in to a verified session in the refresh cookie.
  const cookie = enabled.headers.getSetCookie().map((c) => c.split(";")[0]).join("; ");
  const session = await json(await post("/auth/refresh", {}, { Cookie: cookie }), "refresh after two-factor");
  const admin = { ...CSRF, Authorization: `Bearer ${session.access_token}`, "Content-Type": "application/json" };
  const call = async (method: string, path: string, body?: unknown) =>
    json(
      await fetch(`${API_URL}/api/v1${path}`, { method, headers: admin, body: body ? JSON.stringify(body) : undefined }),
      `${method} ${path}`,
    );

  // For steps that need the operator (the load test's allowance), valid 15 minutes.
  writeFileSync(`${__dirname}/../.platform.json`, JSON.stringify({ adminToken: session.access_token }), { mode: 0o600 });

  await call("POST", "/admin/ai/models/sync", { provider: "gemini" });
  const models: any[] = await call("GET", "/admin/ai/models");
  for (const name of [CHAT_MODEL, EMBEDDING_MODEL]) {
    const model = models.find((m) => m.provider === "gemini" && m.model_id === name);
    if (!model) throw new Error(`model sync did not list ${name}`);
    if (name === CHAT_MODEL && !model.current_price) {
      await call("POST", `/admin/ai/models/${model.id}/prices`, {
        input_per_mtok: "0.30",
        output_per_mtok: "2.50",
        currency: "USD",
        source_note: "E2E test price",
      });
    }
    if (!model.active) await call("PATCH", `/admin/ai/models/${model.id}`, { active: true });
  }
}
