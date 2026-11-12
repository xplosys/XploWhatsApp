"use client";

import Link from "next/link";
import { useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/Field";
import { api, errorMessage } from "@/lib/api/client";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/password/forgot", { method: "POST", body: { email }, auth: false });
      setSent(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Reset your password</h1>
        <p className="mt-1 text-sm text-muted">We&apos;ll email you a link to choose a new password.</p>
      </div>
      {error && <Alert tone="error">{error}</Alert>}
      {sent ? (
        <Alert tone="success">If an account exists for {email}, a reset link is on its way. It is valid for one hour.</Alert>
      ) : (
        <>
          <TextField label="Email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          <Button type="submit" loading={busy} className="w-full">
            Send reset link
          </Button>
        </>
      )}
      <Link href="/login" className="block text-center text-sm text-brand hover:underline">
        Back to sign in
      </Link>
    </form>
  );
}
