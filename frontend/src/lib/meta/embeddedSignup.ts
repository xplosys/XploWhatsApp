// Meta WhatsApp Embedded Signup in the browser.
//
// The Facebook JS SDK opens Meta's own sign-up popup. When the business finishes,
// Meta returns a short-lived code (about 30 seconds) through FB.login and posts the
// selected WABA / phone number IDs as a window message. Both go straight to our
// backend, which exchanges the code server-side and re-verifies every ID.
// Docs: https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation/

import type { SignupConfig } from "@/lib/api/types";

interface FBLoginResponse {
  authResponse?: { code?: string } | null;
  status?: string;
}

interface FBStatic {
  init(params: { appId: string; autoLogAppEvents: boolean; xfbml: boolean; version: string }): void;
  login(cb: (r: FBLoginResponse) => void, opts: Record<string, unknown>): void;
}

declare global {
  interface Window {
    FB?: FBStatic;
    fbAsyncInit?: () => void;
  }
}

export interface SignupSessionInfo {
  event: string; // FINISH, FINISH_ONLY_WABA, FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING, CANCEL, ...
  waba_id?: string;
  phone_number_id?: string;
  business_id?: string;
  current_step?: string;
  error_message?: string;
  error_code?: string;
  session_id?: string;
}

export type SignupOutcome =
  | { kind: "finished"; code: string; info: SignupSessionInfo }
  | { kind: "cancelled"; step?: string; error?: string };

let sdkPromise: Promise<FBStatic> | null = null;

export function loadFacebookSdk(config: SignupConfig): Promise<FBStatic> {
  if (!config.app_id) return Promise.reject(new Error("Meta app is not configured."));
  if (sdkPromise) return sdkPromise;
  sdkPromise = new Promise<FBStatic>((resolve, reject) => {
    window.fbAsyncInit = () => {
      if (!window.FB) return reject(new Error("Facebook SDK failed to start."));
      window.FB.init({ appId: config.app_id!, autoLogAppEvents: true, xfbml: true, version: config.graph_api_version });
      resolve(window.FB);
    };
    const script = document.createElement("script");
    script.src = "https://connect.facebook.net/en_US/sdk.js";
    script.async = true;
    script.defer = true;
    script.crossOrigin = "anonymous";
    script.onerror = () => {
      sdkPromise = null;
      reject(new Error("Could not load Facebook's sign-up script. Check that it is not blocked by an extension."));
    };
    document.body.appendChild(script);
  });
  return sdkPromise;
}

function extrasFor(version: string): Record<string, unknown> {
  // v4: products come from the Login for Business configuration; extras.setup stays empty.
  // v5 and previews: the version is named explicitly.
  return version === "v4" ? { setup: {} } : { setup: {}, version };
}

function parseMessage(event: MessageEvent): SignupSessionInfo | null {
  if (typeof event.origin !== "string" || !event.origin.endsWith("facebook.com")) return null;
  try {
    const data = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
    if (data?.type !== "WA_EMBEDDED_SIGNUP") return null;
    return { event: String(data.event ?? ""), ...(data.data ?? {}) };
  } catch {
    return null;
  }
}

/**
 * Must be called directly from a click handler: browsers only allow the popup
 * when it opens in response to a user gesture.
 */
export function launchEmbeddedSignup(fb: FBStatic, config: SignupConfig): Promise<SignupOutcome> {
  return new Promise((resolve) => {
    let info: SignupSessionInfo | null = null;
    let code: string | null = null;
    let settled = false;

    const finish = (outcome: SignupOutcome) => {
      if (settled) return;
      settled = true;
      window.removeEventListener("message", onMessage);
      resolve(outcome);
    };
    const maybeFinish = () => {
      if (code && info?.waba_id) finish({ kind: "finished", code, info });
    };
    const onMessage = (event: MessageEvent) => {
      const parsed = parseMessage(event);
      if (!parsed) return;
      info = parsed;
      if (parsed.event === "CANCEL" || parsed.event === "ERROR") {
        finish({ kind: "cancelled", step: parsed.current_step, error: parsed.error_message });
        return;
      }
      maybeFinish();
    };
    window.addEventListener("message", onMessage);

    fb.login(
      (response) => {
        code = response.authResponse?.code ?? null;
        if (!code) {
          finish({ kind: "cancelled", step: info?.current_step, error: info?.error_message });
          return;
        }
        maybeFinish();
        // The session info message normally arrives first; give it a moment if not.
        window.setTimeout(() => {
          if (!settled) {
            finish(
              info?.waba_id
                ? { kind: "finished", code: code!, info }
                : { kind: "cancelled", error: "Meta did not say which WhatsApp account was selected. Please try again." },
            );
          }
        }, 4000);
      },
      {
        config_id: config.config_id,
        response_type: "code",
        override_default_response_type: true,
        extras: extrasFor(config.embedded_signup_version),
      },
    );
  });
}
