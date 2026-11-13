"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { SelectField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type { DataExport, SecuritySettings } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatDate, formatDateTime } from "@/lib/locale";

const RETENTION = [
  { value: "", label: "Keep messages while the business exists" },
  { value: "30", label: "Delete messages after 30 days" },
  { value: "90", label: "Delete messages after 90 days" },
  { value: "180", label: "Delete messages after 6 months" },
  { value: "365", label: "Delete messages after 1 year" },
  { value: "730", label: "Delete messages after 2 years" },
];

const EXPORT_TONE = { queued: "neutral", running: "brand", ready: "success", failed: "danger", expired: "neutral" } as const;
const EXPORT_LABEL = { queued: "Waiting", running: "Preparing", ready: "Ready", failed: "Failed", expired: "Expired" };

function formatSize(bytes: number | null): string {
  if (!bytes) return "";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function SecurityCard({ settings, onSaved }: { settings: SecuritySettings; onSaved: (s: SecuritySettings) => void }) {
  const { me } = useAuth();
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ tone: "error" | "success"; text: string } | null>(null);

  async function save(key: string, body: Partial<SecuritySettings>, done: string) {
    setBusy(key);
    setMsg(null);
    try {
      onSaved(await api<SecuritySettings>("/tenants/current/security", { method: "PUT", body }));
      setMsg({ tone: "success", text: done });
    } catch (err) {
      setMsg({ tone: "error", text: errorMessage(err) });
    } finally {
      setBusy(null);
    }
  }

  const canRequire = !!me?.mfa_verified;
  return (
    <Card
      title="Two-factor sign-in for the whole team"
      description="Everyone must use an authenticator app code to open this business. People without it are asked to turn it on first."
      actions={<Badge tone={settings.require_mfa ? "success" : "neutral"}>{settings.require_mfa ? "Required" : "Optional"}</Badge>}
    >
      <div className="space-y-4">
        {msg && <Alert tone={msg.tone}>{msg.text}</Alert>}
        {settings.members_without_mfa > 0 && (
          <p className="text-sm text-muted">
            {settings.members_without_mfa} team member{settings.members_without_mfa === 1 ? " hasn't" : "s haven't"} turned on
            two-factor yet{settings.require_mfa ? " and can't open the business until they do." : "."}
          </p>
        )}
        {settings.require_mfa ? (
          <Button variant="secondary" loading={busy === "mfa"} onClick={() => save("mfa", { require_mfa: false }, "Two-factor is now optional.")}>
            Make it optional
          </Button>
        ) : canRequire ? (
          <Button loading={busy === "mfa"} onClick={() => save("mfa", { require_mfa: true }, "Two-factor is now required for everyone.")}>
            Require two-factor
          </Button>
        ) : (
          <Alert tone="info">
            Turn on two-factor for your own account on the Account &amp; security page, then sign in again with it, before requiring it
            for everyone.
          </Alert>
        )}
        <div className="border-t border-line pt-4">
          <SelectField
            label="Message history"
            hint="Older WhatsApp messages are deleted every hour. Contacts, leads and notes are kept."
            value={settings.message_retention_days?.toString() ?? ""}
            options={RETENTION}
            disabled={busy === "retention"}
            onChange={(e) =>
              save(
                "retention",
                { message_retention_days: e.target.value ? Number(e.target.value) : null },
                e.target.value ? "Messages older than that will be deleted within the hour." : "Messages are kept.",
              )
            }
          />
        </div>
      </div>
    </Card>
  );
}

function ExportsCard() {
  const [exports, setExports] = useState<DataExport[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api<DataExport[]>("/privacy/exports").then(
        (rows) => setExports(rows),
        (err) => setError(errorMessage(err)),
      ),
    [],
  );
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);
  const pending = exports?.some((e) => e.status === "queued" || e.status === "running");
  useEffect(() => {
    if (!pending) return;
    const t = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(t);
  }, [pending, load]);

  async function request() {
    setBusy("new");
    setError(null);
    try {
      await api("/privacy/exports", { method: "POST" });
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function download(id: string) {
    setBusy(id);
    setError(null);
    try {
      const { url } = await api<{ url: string }>(`/privacy/exports/${id}/download`, { method: "POST" });
      window.location.assign(url);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card
      title="Export your data"
      description="Everything this business holds (contacts, conversations, leads, knowledge, settings, invoices and the audit log) as a zip of JSON files. API keys and WhatsApp tokens are left out."
      actions={
        <Button onClick={request} loading={busy === "new"} disabled={!!pending}>
          Request an export
        </Button>
      }
    >
      {error && <Alert tone="error">{error}</Alert>}
      {exports && exports.length === 0 && <p className="text-sm text-muted">No exports yet. We email you when one is ready.</p>}
      {exports && exports.length > 0 && (
        <ul className="divide-y divide-line">
          {exports.map((e) => (
            <li key={e.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
              <div>
                <p className="flex items-center gap-2 text-sm font-medium">
                  Requested {formatDateTime(e.created_at)}
                  <Badge tone={EXPORT_TONE[e.status]}>{EXPORT_LABEL[e.status]}</Badge>
                </p>
                <p className="text-xs text-muted">
                  {e.status === "ready" && `${formatSize(e.size_bytes)} · available until ${formatDate(e.expires_at)}`}
                  {e.status === "failed" && e.error}
                  {(e.status === "queued" || e.status === "running") && "Large businesses can take a few minutes."}
                </p>
              </div>
              {e.status === "ready" && (
                <Button variant="secondary" loading={busy === e.id} onClick={() => download(e.id)}>
                  Download
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function DeleteBusinessCard({ name }: { name: string }) {
  const { logout } = useAuth();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ password: "", confirm_name: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/tenants/current/delete", { method: "POST", body: form });
      await logout();
      router.replace("/login");
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Card title="Delete this business" description="For closing your account with XploSys.">
      {!open ? (
        <Button variant="danger" onClick={() => setOpen(true)}>
          Delete {name}
        </Button>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4">
          <Alert tone="warning">
            Everyone loses access at once and the assistant stops replying. All data is permanently removed after 30 days; until then
            XploSys support can restore it. Tax invoices and the audit log are kept as the law requires. Cancel any paid plan on the
            Billing page first, and export your data if you want a copy.
          </Alert>
          {error && <Alert tone="error">{error}</Alert>}
          <div className="grid gap-4 md:grid-cols-2">
            <TextField
              label={`Type the business name (${name})`}
              value={form.confirm_name}
              onChange={(e) => setForm({ ...form, confirm_name: e.target.value })}
              autoComplete="off"
              required
            />
            <TextField
              label="Your password"
              type="password"
              autoComplete="current-password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              required
            />
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              type="submit"
              variant="danger"
              loading={busy}
              disabled={!form.password || form.confirm_name.trim().toLowerCase() !== name.trim().toLowerCase()}
            >
              Delete the business
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

export default function PrivacyPage() {
  const { me, can } = useAuth();
  const [settings, setSettings] = useState<SecuritySettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const allowed = can("security:manage");

  useEffect(() => {
    if (!allowed) return;
    api<SecuritySettings>("/tenants/current/security").then(setSettings, (e) => setError(errorMessage(e)));
  }, [allowed]);

  if (!allowed) return <Alert tone="info">Only the business owner can change privacy and security settings.</Alert>;
  if (!settings && !error) return <PageLoader />;
  const name = me?.memberships.find((m) => m.tenant_id === me.active_tenant_id)?.tenant_name ?? "this business";
  return (
    <>
      <header>
        <h1 className="text-2xl font-semibold">Privacy &amp; security</h1>
        <p className="mt-1 text-sm text-muted">
          How your team signs in, how long customer messages are kept, and your data under India&apos;s DPDP Act and the GDPR.
        </p>
      </header>
      {error && <Alert tone="error">{error}</Alert>}
      {settings && <SecurityCard settings={settings} onSaved={setSettings} />}
      <ExportsCard />
      <DeleteBusinessCard name={name} />
    </>
  );
}
