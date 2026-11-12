"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { WithToken } from "@/components/TokenPage";
import { Alert } from "@/components/ui/Alert";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/AuthProvider";

function Verify({ token }: { token: string | null }) {
  const { status, reload } = useAuth();
  const [state, setState] = useState<"working" | "done" | "error">(token ? "working" : "error");
  const [message, setMessage] = useState(token ? "" : "This link is missing its token.");
  const started = useRef(false);

  useEffect(() => {
    if (!token || started.current) return;
    started.current = true;
    api("/auth/verify-email", { method: "POST", body: { token }, auth: false })
      .then(async () => {
        setState("done");
        if (status === "authenticated") await reload();
      })
      .catch((err) => {
        setState("error");
        setMessage(errorMessage(err));
      });
  }, [token, status, reload]);

  if (state === "working") return <PageLoader label="Verifying your email" />;
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold">Email verification</h1>
      {state === "done" ? (
        <Alert tone="success">Your email address is verified.</Alert>
      ) : (
        <Alert tone="error">{message} You can request a new link from your dashboard.</Alert>
      )}
      <Link href={status === "authenticated" ? "/dashboard" : "/login"} className="block text-sm text-brand hover:underline">
        Continue
      </Link>
    </div>
  );
}

export default function VerifyEmailPage() {
  return <WithToken render={(token) => <Verify token={token} />} />;
}
