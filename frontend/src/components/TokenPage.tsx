"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { PageLoader } from "@/components/ui/Spinner";

/** Reads ?token= from the URL. useSearchParams needs a Suspense boundary in Next.js. */
function Inner({ render }: { render: (token: string | null) => React.ReactNode }) {
  const params = useSearchParams();
  return <>{render(params.get("token"))}</>;
}

export function WithToken({ render }: { render: (token: string | null) => React.ReactNode }) {
  return (
    <Suspense fallback={<PageLoader />}>
      <Inner render={render} />
    </Suspense>
  );
}
