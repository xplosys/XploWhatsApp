import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";

import { ErrorReporting } from "@/components/ErrorReporting";
import { themeInitScript } from "@/components/ThemeToggle";
import { AuthProvider } from "@/lib/auth/AuthProvider";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "XploWhatsApp", template: "%s · XploWhatsApp" },
  description: "AI-powered WhatsApp customer engagement by XploSys.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#070b14" },
  ],
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Set by src/proxy.ts with the page's Content-Security-Policy. Reading it renders every
  // page per request, which a nonce needs.
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  // Read at request time, so one image serves every environment.
  const sentryDsn = process.env.SENTRY_DSN_WEB;
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: themeInitScript }} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        {/* Same typeface as xplosys.com. */}
        {/* eslint-disable-next-line @next/next/no-page-custom-font */}
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap"
        />
      </head>
      <body>
        {sentryDsn && (
          <ErrorReporting
            dsn={sentryDsn}
            environment={process.env.ENVIRONMENT ?? "development"}
            release={`xplowhatsapp@${process.env.APP_VERSION ?? "dev"}`}
          />
        )}
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
