import { NextResponse, type NextRequest } from "next/server";

// A fresh nonce per page view: only scripts Next.js renders with it (and scripts those
// load, through 'strict-dynamic') can run, so injected markup can't execute.
// Third parties the app uses on purpose:
// - Razorpay Checkout (script, its iframe, its API) on the Billing page.
// - Facebook's SDK and its dialogs for WhatsApp Embedded Signup.
// - Google Fonts (the same typeface as xplosys.com).
// Inline style attributes stay allowed: charts and layout set them, and styles can't run code.
export function buildCsp(nonce: string, dev: boolean): string {
  const directives: Record<string, string[]> = {
    "default-src": ["'self'"],
    "script-src": ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'", ...(dev ? ["'unsafe-eval'"] : [])],
    "style-src": ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
    "font-src": ["'self'", "https://fonts.gstatic.com", "data:"],
    "img-src": ["'self'", "data:", "blob:", "https:"],
    "connect-src": [
      "'self'",
      "https://*.razorpay.com",
      "https://*.facebook.com",
      "https://connect.facebook.net",
      ...(dev ? ["ws:", "wss:"] : []),
    ],
    "frame-src": ["https://api.razorpay.com", "https://checkout.razorpay.com", "https://*.facebook.com"],
    "object-src": ["'none'"],
    "base-uri": ["'self'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'none'"],
  };
  return Object.entries(directives)
    .map(([name, values]) => `${name} ${values.join(" ")}`)
    .join("; ");
}

export function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = buildCsp(nonce, process.env.NODE_ENV === "development");

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    {
      // Pages only: not the API (it sends its own policy) or static files.
      source: "/((?!api|_next/static|_next/image|favicon.ico|icon|apple-icon).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
