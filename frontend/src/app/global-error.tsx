"use client";

import { useEffect } from "react";

import { reportError } from "@/components/ErrorReporting";

import "./globals.css";

// Shown only if the root layout itself fails, so it brings its own <html> and <body>.
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => reportError(error), [error]);
  return (
    <html lang="en">
      <body>
        <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-4 px-4 text-center">
          <h1 className="text-2xl font-semibold">XploWhatsApp couldn&apos;t load</h1>
          <p className="text-sm text-muted">
            Our team has been told. Please try again in a moment.
            {error.digest && <span className="mt-2 block font-mono text-xs">Reference: {error.digest}</span>}
          </p>
          <button
            type="button"
            onClick={() => reset()}
            className="rounded-full bg-brand px-5 py-2 text-sm font-semibold text-white"
          >
            Try again
          </button>
        </main>
      </body>
    </html>
  );
}
