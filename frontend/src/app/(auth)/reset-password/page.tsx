"use client";

import Link from "next/link";
import { useState } from "react";

import { WithToken } from "@/components/TokenPage";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/Field";
import { api, errorMessage } from "@/lib/api/client";

function ResetForm({ token }: { token: string | null }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(token ? null : "This link is missing its token.");
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api("/auth/password/reset", { method: "POST", body: { token, password }, auth: false });
      setDone(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div className="space-y-5">
        <Alert tone="success">Your password was changed and you were signed out everywhere.</Alert>
        <Link href="/login" className="block text-center text-sm text-brand hover:underline">
          Sign in
        </Link>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="space-y-5">
      <h1 className="text-xl font-semibold">Choose a new password</h1>
      {error && <Alert tone="error">{error}</Alert>}
      <TextField label="New password" type="password" autoComplete="new-password" hint="At least 10 characters." value={password} onChange={(e) => setPassword(e.target.value)} required />
      <TextField label="Repeat new password" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
      <Button type="submit" loading={busy} disabled={!token} className="w-full">
        Update password
      </Button>
    </form>
  );
}

export default function ResetPasswordPage() {
  return <WithToken render={(token) => <ResetForm token={token} />} />;
}
