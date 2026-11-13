"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { reportError } from "@/components/ErrorReporting";
import { Button } from "@/components/ui/Button";

export default function ErrorPage({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  const router = useRouter();
  useEffect(() => reportError(error), [error]);
  return (
    <main className="mx-auto flex min-h-[60vh] max-w-md flex-col items-center justify-center gap-4 px-4 text-center">
      <h1 className="text-2xl font-semibold">Something went wrong</h1>
      <p className="text-sm text-muted">
        This page hit an unexpected error and our team has been told. Try again, or go back to the overview.
        {error.digest && <span className="mt-2 block font-mono text-xs">Reference: {error.digest}</span>}
      </p>
      <div className="flex gap-2">
        <Button onClick={() => reset()}>Try again</Button>
        <Button variant="secondary" onClick={() => router.push("/dashboard")}>
          Go to overview
        </Button>
      </div>
    </main>
  );
}
