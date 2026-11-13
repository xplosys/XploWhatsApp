/** Steps several journeys share, done through the UI as a business owner would. */
import { expect, type Page } from "@playwright/test";
import { blockThirdParty, fakes, stubFacebookSdk } from "./support";

export type Owner = { name: string; business: string; email: string; password: string };

export function newOwner(business: string, name = "Meera Nair"): Owner {
  const stamp = `${Date.now()}${Math.floor(Math.random() * 1000)}`;
  const slug = business.toLowerCase().replace(/[^a-z]+/g, "-");
  return { name, business, email: `owner${stamp}@${slug}.example.com`, password: `Clinic-Pass-${stamp}` };
}

/** Fresh fakes, a stubbed Facebook SDK, and a record of uncaught browser errors. */
export async function preparePage(page: Page): Promise<string[]> {
  const ids = await fakes.reset();
  await blockThirdParty(page);
  await stubFacebookSdk(page, ids);
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  return errors;
}

export async function register(page: Page, owner: Owner) {
  await page.goto("/register");
  await page.getByLabel("Your name").fill(owner.name);
  await page.getByLabel("Business name").fill(owner.business);
  await page.getByLabel("Work email").fill(owner.email);
  await page.getByLabel("Password").fill(owner.password);
  await page.getByRole("button", { name: "Create account" }).click();
  await page.waitForURL("**/dashboard");
  await expect(page.getByText(owner.business).first()).toBeVisible();
}

export async function connectWhatsApp(page: Page) {
  await page.getByRole("link", { name: "WhatsApp", exact: true }).click();
  await page.getByRole("button", { name: "Connect WhatsApp" }).click();
  await expect(page.getByText("+91 98765 43210").first()).toBeVisible();
  const meta = await fakes.meta();
  expect(meta.subscribed, "the app subscribed to the account's webhooks").toBe(true);
}

export async function setUpGeminiAssistant(page: Page, about: string) {
  await page.getByRole("link", { name: "AI assistant" }).click();
  const gemini = page.locator("form").filter({ has: page.getByRole("heading", { name: "Google Gemini" }) });
  await gemini.getByLabel("Key", { exact: true }).selectOption("platform");
  await gemini.getByLabel(/Use Google Gemini/).check();
  await gemini.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Google Gemini settings saved.")).toBeVisible();
  await page.getByLabel("About your business").fill(about);
  await page.getByLabel("Opening hours").fill("Monday to Saturday, 9am to 7pm");
  await page.getByRole("button", { name: "Save new version" }).click();
  await expect(page.getByText(/Version 1/).first()).toBeVisible();
}

export async function turnOnAutoReplies(page: Page) {
  await page.getByRole("link", { name: "AI assistant" }).click();
  await page.getByRole("button", { name: "Turn on automatic replies" }).click();
  await expect(page.getByText("Replying automatically")).toBeVisible();
}
