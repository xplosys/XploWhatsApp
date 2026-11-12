"use client";

import QRCode from "qrcode";
import { useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { TextField } from "@/components/ui/Field";
import { api, errorMessage } from "@/lib/api/client";
import type { SessionResponse } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";

function ChangePassword() {
  const { acceptSession } = useAuth();
  const [form, setForm] = useState({ current_password: "", new_password: "" });
  const [msg, setMsg] = useState<{ tone: "error" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      await acceptSession(await api<SessionResponse>("/auth/password/change", { method: "POST", body: form }));
      setForm({ current_password: "", new_password: "" });
      setMsg({ tone: "success", text: "Password changed. Other devices were signed out." });
    } catch (err) {
      setMsg({ tone: "error", text: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Password">
      <form onSubmit={onSubmit} className="space-y-4">
        {msg && <Alert tone={msg.tone}>{msg.text}</Alert>}
        <TextField label="Current password" type="password" autoComplete="current-password" value={form.current_password} onChange={(e) => setForm({ ...form, current_password: e.target.value })} required />
        <TextField label="New password" type="password" autoComplete="new-password" hint="At least 10 characters." value={form.new_password} onChange={(e) => setForm({ ...form, new_password: e.target.value })} required />
        <Button type="submit" loading={busy}>
          Change password
        </Button>
      </form>
    </Card>
  );
}

function TwoFactor() {
  const { me, reload } = useAuth();
  const [setup, setSetup] = useState<{ secret: string; qr: string } | null>(null);
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [recovery, setRecovery] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!me) return null;

  async function step(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const start = () =>
    step(async () => {
      const res = await api<{ secret: string; otpauth_uri: string }>("/auth/mfa/setup", { method: "POST" });
      setSetup({ secret: res.secret, qr: await QRCode.toDataURL(res.otpauth_uri, { margin: 1, width: 192 }) });
    });

  const enable = () =>
    step(async () => {
      const res = await api<{ recovery_codes: string[] }>("/auth/mfa/enable", { method: "POST", body: { code } });
      setRecovery(res.recovery_codes);
      setSetup(null);
      setCode("");
      await reload();
    });

  const disable = () =>
    step(async () => {
      await api("/auth/mfa/disable", { method: "POST", body: { password, code } });
      setPassword("");
      setCode("");
      await reload();
    });

  return (
    <Card
      title="Two-factor authentication"
      description="Protect your account with a code from an authenticator app such as Google Authenticator or Microsoft Authenticator."
      actions={<Badge tone={me.user.mfa_enabled ? "success" : "warning"}>{me.user.mfa_enabled ? "On" : "Off"}</Badge>}
    >
      <div className="space-y-4">
        {error && <Alert tone="error">{error}</Alert>}
        {recovery && (
          <Alert tone="warning">
            <p className="font-medium">Save these recovery codes now. Each works once, and they won&apos;t be shown again.</p>
            <ul className="mt-2 grid grid-cols-2 gap-1 font-mono text-xs">
              {recovery.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </Alert>
        )}
        {!me.user.mfa_enabled && !setup && (
          <Button onClick={start} loading={busy}>
            Set up two-factor authentication
          </Button>
        )}
        {setup && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-6">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={setup.qr} alt="QR code for your authenticator app" width={192} height={192} className="rounded-control border border-line" />
              <div className="text-sm">
                <p className="text-muted">Scan the code, or enter this key manually:</p>
                <p className="mt-1 break-all font-mono">{setup.secret}</p>
              </div>
            </div>
            <TextField label="6-digit code from the app" inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} />
            <Button onClick={enable} loading={busy} disabled={code.trim().length < 6}>
              Turn on
            </Button>
          </div>
        )}
        {me.user.mfa_enabled && !me.user.is_super_admin && (
          <div className="grid gap-4 md:grid-cols-[1fr_1fr_auto] md:items-end">
            <TextField label="Password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
            <TextField label="Code" inputMode="numeric" value={code} onChange={(e) => setCode(e.target.value)} />
            <Button variant="danger" onClick={disable} loading={busy} disabled={!password || code.trim().length < 6}>
              Turn off
            </Button>
          </div>
        )}
        {me.user.mfa_enabled && me.user.is_super_admin && (
          <p className="text-sm text-muted">Super Admin accounts must keep two-factor authentication on.</p>
        )}
      </div>
    </Card>
  );
}

export default function AccountPage() {
  const { me } = useAuth();
  if (!me) return null;
  return (
    <>
      <header>
        <h1 className="text-2xl font-semibold">Account &amp; security</h1>
        <p className="mt-1 text-sm text-muted">
          {me.user.full_name} · {me.user.email}
        </p>
      </header>
      <TwoFactor />
      <ChangePassword />
    </>
  );
}
