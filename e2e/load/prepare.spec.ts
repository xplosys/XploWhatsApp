/**
 * Sets up the business a load test sends its traffic to, through the UI like a real
 * owner: register, connect WhatsApp, Gemini assistant, automatic replies on. Writes the
 * owner's API token to load/.target.json for k6 (it lasts the access token's lifetime).
 */
import { readFileSync, writeFileSync } from "node:fs";
import { test } from "@playwright/test";
import { connectWhatsApp, newOwner, preparePage, register, setUpGeminiAssistant, turnOnAutoReplies } from "../tests/steps";
import { API_URL, CSRF, api, json } from "../tests/support";

// The free trial allows 500 AI replies a month; after that the assistant stops and
// conversations go to the team. A load test sends thousands, so on this throwaway stack
// the operator raises the trial's allowance (an admin plan edit, as in production).
async function raiseTrialAllowance() {
  const { adminToken } = JSON.parse(readFileSync(`${__dirname}/../.platform.json`, "utf8"));
  const plans: any[] = await api(adminToken, "GET", "/admin/billing/plans");
  const trial = plans.find((p) => p.code === "trial");
  await api(adminToken, "PATCH", `/admin/billing/plans/${trial.id}`, { limits: { monthly_ai_messages: 1_000_000 } });
}

test("prepare a connected business with automatic replies", async ({ page }) => {
  await raiseTrialAllowance();
  const owner = newOwner("Load Test Dental");
  await preparePage(page);
  await register(page, owner);
  await connectWhatsApp(page);
  await setUpGeminiAssistant(page, "Load Test Dental is a dental clinic used for load testing.");
  await turnOnAutoReplies(page);
  const session = await json(
    await fetch(`${API_URL}/api/v1/auth/login`, {
      method: "POST",
      headers: { ...CSRF, "Content-Type": "application/json" },
      body: JSON.stringify({ email: owner.email, password: owner.password }),
    }),
    "owner sign-in",
  );
  writeFileSync(`${__dirname}/.target.json`, JSON.stringify({ token: session.access_token, email: owner.email }));
});
