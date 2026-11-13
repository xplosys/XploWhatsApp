import { ApiError } from "@/lib/api/client";
import type { PlanPrice } from "@/lib/api/types";

export function formatMoney(amount: string | number, currency: string): string {
  return new Intl.NumberFormat(currency === "INR" ? "en-IN" : undefined, {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(Number(amount));
}

export function priceLabel(p: PlanPrice): string {
  return `${formatMoney(p.amount_with_tax, p.currency)} / ${p.interval === "month" ? "month" : "year"}`;
}

export function taxLabel(p: PlanPrice): string {
  return Number(p.tax_rate) > 0 ? `incl. ${Number(p.tax_rate)}% GST` : "no tax added";
}

/** The reasons a plan doesn't fit, when the API sends them. */
export function errorDetails(err: unknown): string[] {
  if (err instanceof ApiError && Array.isArray(err.details)) return err.details.map(String);
  return [];
}

type RazorpayResponse = {
  razorpay_payment_id: string;
  razorpay_subscription_id: string;
  razorpay_signature: string;
};

type RazorpayInstance = { open: () => void; on: (event: string, cb: (r: unknown) => void) => void };

declare global {
  interface Window {
    Razorpay?: new (options: Record<string, unknown>) => RazorpayInstance;
  }
}

const CHECKOUT_JS = "https://checkout.razorpay.com/v1/checkout.js";

/** Loads Razorpay's Checkout script once, only when someone pays. */
function loadRazorpay(): Promise<void> {
  if (window.Razorpay) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${CHECKOUT_JS}"]`);
    const script = existing ?? document.createElement("script");
    script.addEventListener("load", () => resolve());
    script.addEventListener("error", () => reject(new Error("Razorpay Checkout could not be loaded.")));
    if (!existing) {
      script.src = CHECKOUT_JS;
      script.async = true;
      document.body.appendChild(script);
    }
  });
}

/** Opens Razorpay Checkout for a subscription. Resolves with the signed result, or null if closed. */
export async function payWithRazorpay(options: Record<string, unknown>): Promise<RazorpayResponse | null> {
  await loadRazorpay();
  return new Promise((resolve, reject) => {
    const rzp = new window.Razorpay!({
      ...options,
      theme: { color: "#4f46e5" },
      handler: (r: RazorpayResponse) => resolve(r),
      modal: { ondismiss: () => resolve(null) },
    });
    rzp.on("payment.failed", (r) => {
      const reason = (r as { error?: { description?: string } }).error?.description;
      reject(new Error(reason ?? "The payment failed. Try again or use another method."));
    });
    rzp.open();
  });
}
