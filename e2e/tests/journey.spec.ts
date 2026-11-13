/**
 * The whole customer journey in a real browser, against the real API, workers, database,
 * Redis and object storage. Only Meta and Gemini are simulated (e2e/fakes/server.py).
 *
 *   register → connect WhatsApp → set up the Gemini assistant → upload a PDF price list
 *   → a customer asks on WhatsApp → the assistant answers from the PDF → the answer is
 *   sent through the WhatsApp API → usage and the dashboard count it
 */
import { expect, test, type Page } from "@playwright/test";
import { CHAT_MODEL } from "./platform-setup";
import { connectWhatsApp, newOwner, preparePage, register, setUpGeminiAssistant, turnOnAutoReplies } from "./steps";
import { fakes } from "./support";

const owner = newOwner("Sunrise Dental Clinic");
const CUSTOMER = { wa_id: "919812345678", name: "Priya Sharma" };
// The fake embedding model is a bag of words, so the question shares words with the
// PDF to clear the business's default 0.30 relevance threshold (it scores about 0.45).
const QUESTION = "How much does scale and polish cost at Sunrise Dental Clinic?";
// Only the PDF knows this price and duration: the reply passes the grounding check
// (prices must come from the business) only if retrieval found the PDF.
const ANSWER = "Hi Priya! A scale and polish costs Rs 1850 and takes about 40 minutes.";

async function priceListPdf(page: Page): Promise<Buffer> {
  const html = `<html><body style="font-family: sans-serif; padding: 40px">
    <h1>Sunrise Dental Clinic: treatment prices 2026</h1>
    <h2>Cleaning</h2>
    <p>Scale and polish costs Rs 1850 and takes about 40 minutes. Book at least a day ahead.</p>
    <h2>Fillings</h2>
    <p>A tooth-coloured composite filling costs Rs 2400 per tooth.</p>
    <h2>Whitening</h2>
    <p>In-clinic teeth whitening costs Rs 12500 for one 90-minute session.</p>
  </body></html>`;
  const pdfPage = await page.context().newPage();
  await pdfPage.setContent(html);
  const pdf = await pdfPage.pdf({ format: "A4" });
  await pdfPage.close();
  return pdf;
}

test("a clinic signs up, connects WhatsApp, and its assistant answers a customer from a PDF", async ({ page }) => {
  const pageErrors = await preparePage(page);

  await test.step("register", () => register(page, owner));
  await test.step("connect WhatsApp with Embedded Signup", () => connectWhatsApp(page));
  await test.step("set up the assistant with XploSys's Gemini key", () =>
    setUpGeminiAssistant(page, "Sunrise Dental Clinic is a family dental clinic in Pune."),
  );

  await test.step("upload a PDF price list to the knowledge base", async () => {
    const pdf = await priceListPdf(page);
    await page.getByRole("link", { name: "Knowledge base" }).click();
    await page.getByRole("button", { name: "Save settings" }).click();
    await expect(page.getByText("Knowledge settings saved.")).toBeVisible();
    await page.locator("#knowledge-file").setInputFiles({ name: "price-list-2026.pdf", mimeType: "application/pdf", buffer: pdf });
    await page.getByRole("button", { name: "Add", exact: true }).click();
    await expect(page.getByText("price-list-2026").first()).toBeVisible();
    // Read, split and embedded by the knowledge worker.
    await expect(page.getByText("Ready").first()).toBeVisible({ timeout: 90_000 });
  });

  await test.step("turn on automatic replies", () => turnOnAutoReplies(page));

  await test.step("a customer asks on WhatsApp and the assistant answers from the PDF", async () => {
    await fakes.answer("gemini", ANSWER);
    await fakes.customerSays(QUESTION, CUSTOMER);
    await page.getByRole("link", { name: "Conversations" }).click();
    await page.getByText(CUSTOMER.name).first().click();
    await expect(page.getByText(QUESTION).last()).toBeVisible();
    await expect(page.getByText(ANSWER).last()).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText(new RegExp(`AI \\(${CHAT_MODEL}\\)`)).first()).toBeVisible();
    await expect(page.getByText(/Sources: price-list-2026/).first()).toBeVisible();

    // Gemini was given the PDF's passage as reference material.
    const calls = await fakes.aiCalls("gemini");
    expect(JSON.stringify(calls.at(-1))).toContain("Scale and polish costs Rs 1850");
    // And the reply went out through the WhatsApp Cloud API to the customer.
    const sent = (await fakes.meta()).sent;
    const reply = sent.find((m) => m.to === CUSTOMER.wa_id && m.text?.body === ANSWER);
    expect(reply, "reply sent through the WhatsApp API").toBeTruthy();
  });

  await test.step("usage records the reply", async () => {
    await page.getByRole("link", { name: "AI assistant" }).click();
    await expect(page.getByRole("heading", { name: /Usage in/ })).toBeVisible();
    await expect(page.getByText(CHAT_MODEL).first()).toBeVisible();
  });

  await test.step("the dashboard counts the conversation", async () => {
    await page.getByRole("link", { name: "Overview" }).click();
    const week = page.getByRole("region", { name: "Last 7 days" });
    await expect(week).toContainText(/Customer messages\s*1(?!\d)/);
    await expect(week).toContainText(/Handled by the assistant alone\s*100%/);
    await expect(page.getByRole("region", { name: "Inbox" })).toContainText(/Needs a person\s*0(?!\d)/);
    await page.screenshot({ path: "test-results/journey-dashboard.png", fullPage: true });
  });

  expect(pageErrors, "uncaught errors in the browser").toEqual([]);
});
