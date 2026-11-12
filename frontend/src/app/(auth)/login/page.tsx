"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/Field";
import { errorMessage } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/AuthProvider";

export default function LoginPage() {
  const { login, verifyMfa, status } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (status === "authenticated") router.replace("/dashboard");
  }, [status, router]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mfaToken) {
        await verifyMfa(mfaToken, code);
      } else {
        const challenge = await login(email, password);
        if (challenge) setMfaToken(challenge.mfa_token);
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-5" noValidate>
      <div>
        <h1 className="text-xl font-semibold">{mfaToken ? "Two-factor verification" : "Sign in"}</h1>
        <p className="mt-1 text-sm text-muted">
          {mfaToken
            ? "Enter the 6-digit code from your authenticator app, or a recovery code."
            : "Welcome back. Sign in to manage your WhatsApp assistant."}
        </p>
      </div>
      {error && <Alert tone="error">{error}</Alert>}
      {mfaToken ? (
        <TextField
          label="Verification code"
          inputMode="numeric"
          autoComplete="one-time-code"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          required
          autoFocus
        />
      ) : (
        <>
          <TextField label="Email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          <TextField
            label="Password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </>
      )}
      <Button type="submit" loading={busy} className="w-full">
        {mfaToken ? "Verify" : "Sign in"}
      </Button>
      {!mfaToken && (
        <div className="flex justify-between text-sm">
          <Link href="/forgot-password" className="text-brand hover:underline">
            Forgot password?
          </Link>
          <Link href="/register" className="text-brand hover:underline">
            Create an account
          </Link>
        </div>
      )}
    </form>
  );
}
