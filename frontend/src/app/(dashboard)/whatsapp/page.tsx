"use client";

import { useCallback, useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, ApiError, errorMessage } from "@/lib/api/client";
import type { MessageTemplate, SignupConfig, WhatsAppAccount, WhatsAppNumber } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatDateTime } from "@/lib/locale";
import { launchEmbeddedSignup, loadFacebookSdk } from "@/lib/meta/embeddedSignup";
import { placeholderCount, templateFields } from "@/lib/whatsapp/templates";

const STATUS_TONE = {
  connected: "success",
  needs_reconnect: "danger",
  disconnected: "neutral",
  pending: "warning",
} as const;

const HEALTH_TONE = { ok: "success", warning: "warning", error: "danger", unknown: "neutral" } as const;
const QUALITY_TONE: Record<string, "success" | "warning" | "danger" | "neutral"> = {
  GREEN: "success",
  YELLOW: "warning",
  RED: "danger",
};

const STATUS_LABEL: Record<string, string> = {
  connected: "Connected",
  needs_reconnect: "Needs reconnect",
  disconnected: "Disconnected",
  pending: "Not registered",
};

export default function WhatsAppPage() {
  const { can } = useAuth();
  const canManage = can("whatsapp:manage");
  const [config, setConfig] = useState<SignupConfig | null>(null);
  const [accounts, setAccounts] = useState<WhatsAppAccount[] | null>(null);
  const [templates, setTemplates] = useState<MessageTemplate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [pins, setPins] = useState<Record<string, string>>({});

  const fetchAll = useCallback(async () => {
    const [c, a, t] = await Promise.all([
      api<SignupConfig>("/whatsapp/signup-config"),
      api<WhatsAppAccount[]>("/whatsapp/accounts"),
      api<MessageTemplate[]>("/whatsapp/templates"),
    ]);
    return { config: c, accounts: a, templates: t };
  }, []);

  const apply = (d: { config: SignupConfig; accounts: WhatsAppAccount[]; templates: MessageTemplate[] }) => {
    setConfig(d.config);
    setAccounts(d.accounts);
    setTemplates(d.templates);
  };

  useEffect(() => {
    fetchAll()
      .then(apply)
      .catch((err) => setError(errorMessage(err)));
  }, [fetchAll]);

  // Load Meta's SDK early so the popup can open straight from the click.
  useEffect(() => {
    if (config?.enabled && canManage) loadFacebookSdk(config).catch(() => undefined);
  }, [config, canManage]);

  async function run(key: string, fn: () => Promise<unknown>, success?: string) {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await fn();
      if (success) setNotice(success);
      apply(await fetchAll());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function connect() {
    if (!config) return;
    setBusy("connect");
    setError(null);
    setNotice(null);
    try {
      const fb = await loadFacebookSdk(config);
      // Open the popup first (user gesture), create our session in parallel.
      const sessionPromise = api<{ session_id: string }>("/whatsapp/onboarding/sessions", { method: "POST" });
      const outcome = await launchEmbeddedSignup(fb, config);
      const session = await sessionPromise;
      if (outcome.kind === "cancelled") {
        setError(outcome.error ?? "WhatsApp sign-up was closed before it finished.");
        return;
      }
      await api("/whatsapp/onboarding/complete", {
        method: "POST",
        body: {
          session_id: session.session_id,
          code: outcome.code,
          waba_id: outcome.info.waba_id,
          phone_number_id: outcome.info.phone_number_id ?? null,
          business_id: outcome.info.business_id ?? null,
          event: outcome.info.event,
        },
      });
      setNotice("WhatsApp connected. Add a payment method in WhatsApp Manager if you have not yet.");
      apply(await fetchAll());
    } catch (err) {
      setError(err instanceof Error && !(err instanceof ApiError) ? err.message : errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function showPin(number: WhatsAppNumber) {
    setBusy(`pin-${number.id}`);
    try {
      const r = await api<{ pin: string }>(`/whatsapp/numbers/${number.id}/pin`, { method: "POST" });
      setPins((p) => ({ ...p, [number.id]: r.pin }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  if (!accounts || !config) return error ? <Alert tone="error">{error}</Alert> : <PageLoader />;

  const live = accounts.filter((a) => a.status !== "disconnected");
  const connectedNumbers = live.flatMap((a) => (a.status === "connected" ? a.numbers.filter((n) => n.status === "connected") : []));

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">WhatsApp</h1>
          <p className="mt-1 text-sm text-muted">
            Connect your business number through Meta&apos;s official sign-up. Messages use the WhatsApp Cloud API.
          </p>
        </div>
        {canManage && config.enabled && (
          <Button onClick={connect} loading={busy === "connect"}>
            {live.length ? "Connect another number" : "Connect WhatsApp"}
          </Button>
        )}
      </header>

      {error && <Alert tone="error">{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}

      {!config.enabled && (
        <Alert tone="warning">
          WhatsApp sign-up is not available yet because the Meta app is not configured on this server.
          {canManage && config.missing.length > 0 && (
            <> Missing settings: {config.missing.join(", ")}. See docs/META_INTEGRATION.md for the setup steps.</>
          )}
        </Alert>
      )}

      {live.length === 0 ? (
        <Card>
          <EmptyState
            title="No WhatsApp number connected"
            description={
              config.enabled
                ? canManage
                  ? "Use Connect WhatsApp to sign in with Facebook and choose or add your business number."
                  : "Ask an owner or admin to connect your business number."
                : "Once the Meta app is configured, an owner or admin can connect a number here."
            }
          />
        </Card>
      ) : (
        live.map((account) => (
          <Card
            key={account.id}
            title={account.name ?? "WhatsApp Business Account"}
            description={`WABA ${account.waba_id} · last checked ${formatDateTime(account.last_health_check_at)}`}
            actions={
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={STATUS_TONE[account.status]}>{STATUS_LABEL[account.status]}</Badge>
                <Badge tone={HEALTH_TONE[account.health]}>Health: {account.health}</Badge>
              </div>
            }
          >
            {account.status === "needs_reconnect" && (
              <div className="mb-4">
                <Alert tone="error">
                  Meta no longer accepts XploWhatsApp&apos;s access to this account, so messages are paused.
                  {canManage && config.enabled && (
                    <span className="ml-2">
                      <Button variant="secondary" className="py-1" onClick={connect} loading={busy === "connect"}>
                        Reconnect
                      </Button>
                    </span>
                  )}
                </Alert>
              </div>
            )}

            {account.health_issues.length > 0 && (
              <ul className="mb-5 space-y-2">
                {account.health_issues.map((issue) => (
                  <li key={issue.code} className="flex items-start gap-2 text-sm">
                    <Badge tone={issue.level === "error" ? "danger" : issue.level === "warning" ? "warning" : "brand"}>
                      {issue.level}
                    </Badge>
                    <span className="text-ink-2">
                      {issue.message}
                      {issue.code === "payment_method" && (
                        <>
                          {" "}
                          <a
                            className="font-medium text-brand underline"
                            href="https://business.facebook.com/wa/manage/home/"
                            target="_blank"
                            rel="noreferrer"
                          >
                            Open WhatsApp Manager
                          </a>
                        </>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}

            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase tracking-wide text-muted">
                  <tr>
                    <th className="py-2 pr-4">Number</th>
                    <th className="py-2 pr-4">Quality</th>
                    <th className="py-2 pr-4">Limit</th>
                    <th className="py-2 pr-4">Status</th>
                    {canManage && <th className="py-2" />}
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {account.numbers.map((n) => (
                    <tr key={n.id}>
                      <td className="py-3 pr-4">
                        <p className="font-medium">{n.display_phone_number ?? n.phone_number_id}</p>
                        <p className="text-xs text-muted">
                          {n.verified_name ?? "Name pending"}
                          {n.is_coexistence && " · also on WhatsApp Business app"}
                        </p>
                      </td>
                      <td className="py-3 pr-4">
                        <Badge tone={QUALITY_TONE[n.quality_rating ?? ""] ?? "neutral"}>{n.quality_rating ?? "unknown"}</Badge>
                      </td>
                      <td className="py-3 pr-4 text-muted">{n.messaging_limit_tier?.replace("TIER_", "") ?? "—"}</td>
                      <td className="py-3 pr-4">
                        <Badge tone={STATUS_TONE[n.status]}>{STATUS_LABEL[n.status]}</Badge>
                      </td>
                      {canManage && (
                        <td className="py-3 text-right">
                          <div className="flex justify-end gap-2">
                            {n.status === "pending" && account.status === "connected" && (
                              <Button
                                variant="secondary"
                                className="py-1"
                                loading={busy === `reg-${n.id}`}
                                onClick={() =>
                                  run(`reg-${n.id}`, () => api(`/whatsapp/numbers/${n.id}/register`, { method: "POST" }), "Registration requested.")
                                }
                              >
                                Register for sending
                              </Button>
                            )}
                            {n.status === "connected" && !n.is_coexistence && (
                              pins[n.id] ? (
                                <span className="rounded-control bg-surface-muted px-3 py-1 font-mono text-sm" title="Two-step verification PIN">
                                  PIN {pins[n.id]}
                                </span>
                              ) : (
                                <Button variant="ghost" className="py-1" loading={busy === `pin-${n.id}`} onClick={() => showPin(n)}>
                                  Show PIN
                                </Button>
                              )
                            )}
                          </div>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {canManage && (
              <div className="mt-5 flex flex-wrap gap-2 border-t border-line pt-4">
                <Button
                  variant="secondary"
                  loading={busy === `health-${account.id}`}
                  onClick={() => run(`health-${account.id}`, () => api(`/whatsapp/accounts/${account.id}/health-check`, { method: "POST" }), "Health check finished.")}
                >
                  Run health check
                </Button>
                {account.status === "connected" && (
                  <Button
                    variant="secondary"
                    loading={busy === `sync-${account.id}`}
                    onClick={() =>
                      run(
                        `sync-${account.id}`,
                        async () => {
                          const r = await api<{ synced: number }>(`/whatsapp/accounts/${account.id}/templates/sync`, { method: "POST" });
                          setNotice(`Synced ${r.synced} template(s) from Meta.`);
                        },
                      )
                    }
                  >
                    Sync templates
                  </Button>
                )}
                <Button
                  variant="danger"
                  loading={busy === `disc-${account.id}`}
                  onClick={() => {
                    if (window.confirm("Disconnect this WhatsApp account? Automatic replies stop and saved credentials are deleted. Conversation history is kept."))
                      run(`disc-${account.id}`, () => api(`/whatsapp/accounts/${account.id}`, { method: "DELETE" }), "WhatsApp account disconnected.");
                  }}
                >
                  Disconnect
                </Button>
              </div>
            )}
          </Card>
        ))
      )}

      {live.length > 0 && <TemplatesCard templates={templates} />}
      {connectedNumbers.length > 0 && can("conversations:reply") && (
        <SendCard numbers={connectedNumbers} templates={templates.filter((t) => t.status === "APPROVED")} />
      )}
    </div>
  );
}

function TemplatesCard({ templates }: { templates: MessageTemplate[] }) {
  return (
    <Card title="Message templates" description="Approved templates are needed to message a customer more than 24 hours after their last message.">
      {templates.length === 0 ? (
        <EmptyState title="No templates yet" description="Create templates in WhatsApp Manager, then use Sync templates." />
      ) : (
        <ul className="divide-y divide-line">
          {templates.map((t) => (
            <li key={t.id} className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm">
              <div>
                <p className="font-medium">
                  {t.name} <span className="text-muted">· {t.language}</span>
                </p>
                {t.components.find((c) => c.type === "BODY")?.text && (
                  <p className="mt-0.5 line-clamp-2 text-xs text-muted">{t.components.find((c) => c.type === "BODY")?.text}</p>
                )}
                {t.rejected_reason && <p className="mt-0.5 text-xs text-danger">Reason: {t.rejected_reason}</p>}
              </div>
              <div className="flex gap-2">
                {t.category && <Badge tone="brand">{t.category.toLowerCase()}</Badge>}
                <Badge tone={t.status === "APPROVED" ? "success" : t.status === "REJECTED" ? "danger" : "warning"}>
                  {t.status.toLowerCase()}
                </Badge>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function SendCard({ numbers, templates }: { numbers: WhatsAppNumber[]; templates: MessageTemplate[] }) {
  const [numberId, setNumberId] = useState(numbers[0].id);
  const [phone, setPhone] = useState("");
  const [mode, setMode] = useState<"template" | "text">(templates.length ? "template" : "text");
  const [templateId, setTemplateId] = useState(templates[0]?.id ?? "");
  const [params, setParams] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ tone: "success" | "error"; message: string } | null>(null);
  const template = templates.find((t) => t.id === templateId);
  const count = placeholderCount(template);

  async function send(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setResult(null);
    try {
      const body: Record<string, unknown> = { number_id: numberId, phone };
      if (mode === "template" && template) {
        Object.assign(body, templateFields(template, params));
      } else {
        body.text = text;
      }
      await api("/messages", { method: "POST", body });
      setResult({ tone: "success", message: "Message queued. Its delivery status appears in Conversations." });
    } catch (err) {
      setResult({ tone: "error", message: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Send a message" description="Start a conversation with an approved template, or reply with text inside the 24-hour window.">
      <form onSubmit={send} className="grid gap-4 md:grid-cols-2">
        <SelectField
          label="From"
          value={numberId}
          onChange={(e) => setNumberId(e.target.value)}
          options={numbers.map((n) => ({ value: n.id, label: n.display_phone_number ?? n.phone_number_id }))}
        />
        <TextField label="To (WhatsApp number)" placeholder="+91 98765 43210" value={phone} onChange={(e) => setPhone(e.target.value)} required />
        <SelectField
          label="Message type"
          value={mode}
          onChange={(e) => setMode(e.target.value as "template" | "text")}
          options={[
            ...(templates.length ? [{ value: "template", label: "Approved template" }] : []),
            { value: "text", label: "Text (only within 24 hours of their last message)" },
          ]}
        />
        {mode === "template" ? (
          <SelectField
            label="Template"
            value={templateId}
            onChange={(e) => {
              setTemplateId(e.target.value);
              setParams([]);
            }}
            options={templates.map((t) => ({ value: t.id, label: `${t.name} (${t.language})` }))}
          />
        ) : (
          <TextField label="Message" value={text} onChange={(e) => setText(e.target.value)} maxLength={4096} required />
        )}
        {mode === "template" &&
          Array.from({ length: count }, (_, i) => (
            <TextField
              key={i}
              label={`Value for {{${i + 1}}}`}
              value={params[i] ?? ""}
              onChange={(e) => setParams((p) => Object.assign([...p], { [i]: e.target.value }))}
              required
            />
          ))}
        <div className="md:col-span-2 flex flex-wrap items-center gap-3">
          <Button type="submit" loading={busy}>
            Send
          </Button>
          {result && <span className={`text-sm ${result.tone === "error" ? "text-danger" : "text-success"}`}>{result.message}</span>}
        </div>
      </form>
    </Card>
  );
}
