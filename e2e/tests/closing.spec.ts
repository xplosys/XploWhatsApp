/**
 * The assistant greets the customer by name, wraps up politely once the question is
 * answered (asking for feedback) and closes the conversation; the customer's rating
 * reopens it, the assistant thanks them and closes it again, and the rating shows in the
 * inbox.
 */
import { expect, test } from "@playwright/test";
import { connectWhatsApp, newOwner, preparePage, register, setUpGeminiAssistant, turnOnAutoReplies } from "./steps";
import { fakes } from "./support";

const owner = newOwner("Sunrise Dental", "Meera Nair");
const CUSTOMER = { wa_id: "919811122233", name: "Priya Sharma" };
// Set E2E_SCREENSHOTS to a folder to keep pictures of each step.
const SHOTS = process.env.E2E_SCREENSHOTS;

test("the assistant greets by name, asks for feedback and closes the conversation", async ({ page }) => {
  const pageErrors = await preparePage(page);
  const shot = async (name: string) => {
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` });
  };
  await register(page, owner);
  await connectWhatsApp(page);
  await setUpGeminiAssistant(page, "Sunrise Dental is a family dental clinic in Pune.");
  await turnOnAutoReplies(page);

  await test.step("the new settings are on by default", async () => {
    const greet = page.getByLabel(/Greet customers by their name/);
    const closing = page.getByLabel(/Close conversations politely/);
    await expect(greet).toBeChecked();
    await expect(closing).toBeChecked();
    await closing.scrollIntoViewIfNeeded();
    await shot("01-settings");
  });

  await test.step("the first reply greets the customer by name", async () => {
    await fakes.answer("gemini", "Hi Priya! 😊 We're open Monday to Saturday, 9am to 7pm.");
    await fakes.customerSays("Hello, when are you open?", CUSTOMER);
    await page.getByRole("link", { name: "Conversations" }).click();
    await page.getByText(CUSTOMER.name).first().click();
    await expect(page.getByText("Hi Priya! 😊").last()).toBeVisible({ timeout: 60_000 });
    const [call] = await fakes.aiCalls("gemini");
    expect(JSON.stringify(call)).toContain("Priya Sharma");
  });

  await test.step("once answered, it wraps up, asks for feedback and closes", async () => {
    await fakes.answer(
      "gemini",
      "You're welcome, Priya! Is there anything else I can help you with? If not, we'd love your feedback: how would you rate your experience from 1 to 5?",
      { closing: "wrap_up" },
    );
    await fakes.customerSays("Ok thanks", CUSTOMER);
    await expect(page.getByText("Asked for feedback · conversation closed").last()).toBeVisible({ timeout: 60_000 });
    await shot("02-wrap-up-closed");
  });

  await test.step("the customer's rating reopens it; the assistant thanks them and closes it again", async () => {
    await fakes.answer("gemini", "Thank you so much for your feedback, Priya! Have a lovely day. 🌸", {
      closing: "goodbye",
      feedback: { rating: 5, comment: "Very quick and helpful" },
    });
    await fakes.customerSays("5 - very quick and helpful", CUSTOMER);
    await expect(page.getByText("Said goodbye · conversation closed").last()).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText(/Customer feedback: ★★★★★ 5\/5/).last()).toBeVisible();
    await shot("03-feedback-goodbye");
  });

  expect(pageErrors, "uncaught errors in the browser").toEqual([]);
});
