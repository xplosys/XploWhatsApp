/**
 * When the assistant can't help, a person takes over: the assistant hands the
 * conversation to the team, the owner replies from the inbox (sent through the WhatsApp
 * API), then hands the conversation back so the assistant answers the next message.
 */
import { expect, test } from "@playwright/test";
import { connectWhatsApp, newOwner, preparePage, register, setUpGeminiAssistant, turnOnAutoReplies } from "./steps";
import { fakes } from "./support";

const owner = newOwner("Lotus Physiotherapy", "Arjun Mehta");
const CUSTOMER = { wa_id: "919845012345", name: "Rahul Verma" };

test("the assistant hands a complaint to a person, who replies and hands it back", async ({ page }) => {
  const pageErrors = await preparePage(page);
  await register(page, owner);
  await connectWhatsApp(page);
  await setUpGeminiAssistant(page, "Lotus Physiotherapy is a sports injury and rehab clinic in Bengaluru.");
  await turnOnAutoReplies(page);

  await test.step("the assistant hands a complaint to the team", async () => {
    await fakes.answer("gemini", "I'm sorry to hear that, Rahul. Let me get someone from our team to help you.", true);
    await fakes.customerSays("I was charged twice for my last session and I am not happy.", CUSTOMER);
    await page.getByRole("link", { name: "Conversations" }).click();
    await page.getByText(CUSTOMER.name).first().click();
    await expect(page.getByText("Let me get someone from our team to help you.").last()).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText("Needs a person").first()).toBeVisible();
  });

  await test.step("the owner replies from the inbox", async () => {
    const reply = "Hi Rahul, I'm Arjun from Lotus. I've refunded the second charge; it reaches you in 3 to 5 days.";
    await page.getByLabel("Message").fill(reply);
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.getByText(/Sent to WhatsApp/).first()).toBeVisible({ timeout: 30_000 });
    await expect
      .poll(async () => (await fakes.meta()).sent.some((m) => m.to === CUSTOMER.wa_id && m.text?.body === reply))
      .toBe(true);
  });

  await test.step("the owner hands the conversation back to the assistant", async () => {
    await page.getByRole("button", { name: "Hand back to AI" }).click();
    await expect(page.getByText(/AI replies on/i).first()).toBeVisible();
    await fakes.answer("gemini", "You're welcome, Rahul! We're open Monday to Saturday, 9am to 7pm.");
    await fakes.customerSays("Thanks! When are you open?", CUSTOMER);
    await expect(page.getByText("We're open Monday to Saturday, 9am to 7pm.").last()).toBeVisible({ timeout: 60_000 });
  });

  expect(pageErrors, "uncaught errors in the browser").toEqual([]);
});
