"use client";

import { useRouter } from "next/navigation";
import QRCode from "qrcode";
import { useCallback, useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { TextField } from "@/components/ui/Field";
import { ApiError, api, errorMessage } from "@/lib/api/client";
import type { LoginSession, SessionResponse } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatDateTime } from "@/lib/locale";

/** "Chrome on Windows" from a user agent, or the raw string when it isn't recognised. */
function describeDevice(ua: string | null): string {
  if (!ua) return "Unknown device";
  const browser = /Edg\//.test(ua)
    ? "Edge"
    : /OPR\//.test(ua)
      ? "Opera"
      : /Firefox\//.test(ua)
        ? "Firefox"
        : /Chrome\//.test(ua)
          ? "Chrome"
          : /Safari\//.test(ua)
            ? "Safari"
            : null;
  const os = /Windows/.test(ua)
    ? "Windows"
    : /Android/.test(ua)
      ? "Android"
      : /iPhone|iPad/.test(ua)
        ? "iOS"
        : /Mac OS X/.test(ua)
          ? "macOS"
          : /Linux/.test(ua)
            ? "Linux"
            : null;
  if (browser && os) return `${browser} on ${os}`;
  return browser ?? os ?? ua.slice(0, 60);
}

function Sessions() {
  const [sessions, setSessions] = useState<LoginSession[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api<LoginSession[]>("/auth/sessions").then(
        (s) => setSessions(s),
        (err) => setError(errorMessage(err)),
      ),
    [],
  );
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  async function run(key: string, fn: () => Promise<unknown>, done: string) {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(done);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  const others = sessions?.filter((s) => !s.current).length ?? 0;
  return (
    <Card
      title="Where you're signed in"
      description="Signing a device out ends its session at once, not when it next refreshes."
      actions={
        others > 0 && (
          <Button
            variant="secondary"
            loading={busy === "others"}
            onClick={() => run("others", () => api("/auth/sessions/revoke-others", { method: "POST" }), "Signed out of every other device.")}
          >
            Sign out other devices
          </Button>
        )
      }
    >
      {error && <Alert tone="error">{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}
      {sessions && (
        <ul className="divide-y divide-line">
          {sessions.map((s) => (
            <li key={s.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
              <div className="min-w-0">
                <p className="flex items-center gap-2 text-sm font-medium">
                  {describeDevice(s.user_agent)}
                  {s.current && <Badge tone="success">This device</Badge>}
                </p>
                <p className="text-xs text-muted">
                  {s.ip ?? "Unknown address"} · signed in {formatDateTime(s.signed_in_at)} · last active {formatDateTime(s.last_active_at)}
                </p>
              </div>
              {!s.current && (
                <Button
                  variant="ghost"
                  loading={busy === s.id}
                  onClick={() => run(s.id, () => api(`/auth/sessions/${s.id}`, { method: "DELETE" }), "That device was signed out.")}
                >
                  Sign out
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function DeleteAccount() {
  const { me, logout } = useAuth();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<{ text: string; businesses?: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  if (!me || me.user.is_super_admin) return null;

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/me", { method: "DELETE", body: { password, code: code.trim() || undefined } });
      await logout();
      router.replace("/login");
    } catch (err) {
      const details = err instanceof ApiError ? (err.details as { businesses?: string[] } | null | undefined) : null;
      setError({ text: errorMessage(err), businesses: details?.businesses });
      setBusy(false);
    }
  }

  return (
    <Card title="Delete your account" description="Removes your name, email and sign-in details, and takes you out of every business.">
      {!open ? (
        <Button variant="danger" onClick={() => setOpen(true)}>
          Delete my account
        </Button>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4">
          <Alert tone="warning">
            This can&apos;t be undone. Messages you sent to customers stay with the business that sent them, shown as from
            &quot;Deleted user&quot;. If you are the only owner of a business, make someone else an owner or delete the business
            first.
          </Alert>
          {error && (
            <Alert tone="error">
              {error.text}
              {error.businesses && error.businesses.length > 0 && <span className="block font-medium">{error.businesses.join(", ")}</span>}
            </Alert>
          )}
          <div className="grid gap-4 md:grid-cols-2">
            <TextField label="Password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
            {me.user.mfa_enabled && (
              <TextField label="Code from your authenticator app" inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} required />
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="danger" loading={busy} disabled={!password}>
              Delete my account permanently
            </Button>
            <Button type="button" variant="secondary" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </Button>
          </div>
        </form>
      )}
    </Card>
  );
}

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
      <Sessions />
      <DeleteAccount />
    </>
  );
}
