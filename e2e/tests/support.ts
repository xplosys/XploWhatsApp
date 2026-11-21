import { createHmac } from "node:crypto";
import type { Page } from "@playwright/test";

export const API_URL = process.env.E2E_API_URL ?? "http://127.0.0.1:8000";
export const FAKES_URL = process.env.E2E_FAKES_URL ?? "http://127.0.0.1:9000";
export const CSRF = { "X-Requested-With": "XploWhatsApp" };

/** RFC 6238 code (SHA-1, 6 digits, 30 s), as authenticator apps compute it. */
export function totp(base32Secret: string, at = Date.now()): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of base32Secret.replace(/=+$/, "").toUpperCase()) {
    const v = alphabet.indexOf(ch);
    if (v < 0) throw new Error("bad base32 secret");
    bits += v.toString(2).padStart(5, "0");
  }
  const key = Buffer.from(bits.match(/.{8}/g)!.map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 1000 / 30)));
  const mac = createHmac("sha1", key).update(counter).digest();
  const offset = mac[mac.length - 1] & 0xf;
  const code = (mac.readUInt32BE(offset) & 0x7fffffff) % 1_000_000;
  return code.toString().padStart(6, "0");
}

export async function json<T = any>(res: Response, what: string): Promise<T> {
  const text = await res.text();
  if (!res.ok) throw new Error(`${what}: HTTP ${res.status} ${text}`);
  return (text ? JSON.parse(text) : null) as T;
}

/** Calls the API as a signed-in user. */
export async function api<T = any>(token: string, method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_URL}/api/v1${path}`, {
    method,
    headers: { ...CSRF, Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  return json<T>(res, `${method} ${path}`);
}

export const fakes = {
  async reset(): Promise<{ waba_id: string; phone_number_id: string }> {
    return json(await fetch(`${FAKES_URL}/_control/reset`, { method: "POST" }), "reset fakes");
  },
  async meta(): Promise<{ waba_id: string; phone_number_id: string; subscribed: boolean; sent: any[] }> {
    return json(await fetch(`${FAKES_URL}/_control/meta`), "read fake Meta");
  },
  async answer(provider: string, text: string, needsHuman = false): Promise<void> {
    await json(
      await fetch(`${FAKES_URL}/_control/ai/${provider}/answers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, needs_human: needsHuman }),
      }),
      "queue AI answer",
    );
  },
  async aiCalls(provider: string): Promise<any[]> {
    return json(await fetch(`${FAKES_URL}/_control/ai/${provider}/calls`), "read AI calls");
  },
  /** A customer writes to the business on WhatsApp (a signed Meta webhook). */
  async customerSays(text: string, from = { wa_id: "919812345678", name: "Priya" }): Promise<void> {
    const r = await json<{ status: number }>(
      await fetch(`${FAKES_URL}/_control/inbound`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, ...from }),
      }),
      "deliver inbound message",
    );
    if (r.status !== 200) throw new Error(`webhook answered ${r.status}`);
  },
};

/**
 * Stands in for Facebook's JavaScript SDK during Embedded Signup. The real SDK opens
 * Meta's popup; this one answers the way it does when a business finishes signup: the
 * WA_EMBEDDED_SIGNUP message with the chosen account and number, then the login code.
 * Everything after that (code exchange, number lookup, webhook subscription,
 * registration) is the product's real code talking to the fake Graph API.
 */
export async function stubFacebookSdk(page: Page, ids: { waba_id: string; phone_number_id: string }) {
  const sdk = `
    window.FB = {
      init: function () {},
      login: function (callback) {
        setTimeout(function () {
          window.dispatchEvent(new MessageEvent("message", {
            origin: "https://www.facebook.com",
            data: JSON.stringify({ type: "WA_EMBEDDED_SIGNUP", event: "FINISH",
              data: { waba_id: "${ids.waba_id}", phone_number_id: "${ids.phone_number_id}", business_id: "2729063490586005" } })
          }));
          callback({ authResponse: { code: "good-code" }, status: "connected" });
        }, 200);
      }
    };
    setTimeout(function () { window.fbAsyncInit && window.fbAsyncInit(); }, 0);`;
  await page.route(/connect\.facebook\.net/, (route) =>
    route.fulfill({ contentType: "application/javascript", body: sdk }),
  );
}

/** Keeps runs independent of the internet: web fonts fall back to system fonts. */
export async function blockThirdParty(page: Page) {
  await page.route(/fonts\.(googleapis|gstatic)\.com/, (route) => route.abort());
}
