"use client";

import { useCallback, useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField, TextAreaField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type {
  AIModel,
  AIProvider,
  AIUsage,
  Assistant,
  AssistantConfig,
  AssistantVersion,
  ModelChoice,
  PreviewResult,
  ProviderKey,
} from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatDateTime } from "@/lib/locale";

const PROVIDER_LABEL: Record<string, string> = { gemini: "Google Gemini", openai: "OpenAI", deepseek: "DeepSeek" };

const OUTCOME_LABEL: Record<string, string> = {
  ok: "Answered",
  invalid: "Answer rejected",
  skipped: "Skipped",
  provider_timeout: "Timed out",
  provider_rate_limited: "Rate limited",
  provider_unavailable: "Provider error",
  provider_auth_error: "Key rejected",
  provider_billing_error: "Billing problem at provider",
  provider_content_blocked: "Blocked by provider",
  provider_bad_request: "Request rejected",
  provider_incomplete: "Answer cut off",
};

function emptyConfig(models: AIModel[]): AssistantConfig {
  const first = models[0];
  return {
    assistant_name: "Assistant",
    business_description: "",
    instructions: "",
    business_hours: "",
    tone: "friendly",
    reply_language: "match_customer",
    handover_message: "Thanks for your message. A member of our team will reply to you shortly.",
    max_reply_chars: 1000,
    primary: { provider: first?.provider ?? "gemini", model: first?.model_id ?? "" },
    fallbacks: [],
  };
}

interface Data {
  providers: AIProvider[];
  models: AIModel[];
  assistant: Assistant | null;
  versions: AssistantVersion[];
  usage: AIUsage;
}

async function fetchAll(): Promise<Data> {
  const [providers, models, assistant, versions, usage] = await Promise.all([
    api<AIProvider[]>("/ai/providers"),
    api<AIModel[]>("/ai/models"),
    api<Assistant | null>("/ai/assistant"),
    api<AssistantVersion[]>("/ai/assistant/versions"),
    api<AIUsage>("/ai/usage"),
  ]);
  return { providers, models, assistant, versions, usage };
}

export default function AIPage() {
  const { can } = useAuth();
  const allowed = can("ai:configure");
  const [data, setData] = useState<Data | null>(null);
  const [draft, setDraft] = useState<AssistantConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const apply = useCallback((d: Data, resetDraft: boolean) => {
    setData(d);
    if (resetDraft) setDraft(d.assistant?.active_version?.config ?? emptyConfig(d.models));
  }, []);

  useEffect(() => {
    if (!allowed) return;
    fetchAll()
      .then((d) => apply(d, true))
      .catch((err) => setError(errorMessage(err)));
  }, [allowed, apply]);

  async function run(key: string, fn: () => Promise<unknown>, success: string | null, resetDraft = false) {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await fn();
      if (success) setNotice(success);
      apply(await fetchAll(), resetDraft);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  if (!allowed) return <Alert tone="error">Only owners and admins can change the AI assistant.</Alert>;
  if (!data || !draft) return error ? <Alert tone="error">{error}</Alert> : <PageLoader />;

  const assistant = data.assistant;
  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">AI assistant</h1>
          <p className="mt-1 text-sm text-muted">
            Answers your customers on WhatsApp using only the information you give it, and hands over to your team when it can&apos;t help.
          </p>
        </div>
        {assistant && (
          <div className="flex items-center gap-3">
            <Badge tone={assistant.auto_reply_enabled ? "success" : "neutral"}>
              {assistant.auto_reply_enabled ? "Replying automatically" : "Automatic replies off"}
            </Badge>
            <Button
              variant={assistant.auto_reply_enabled ? "secondary" : "primary"}
              loading={busy === "toggle"}
              onClick={() =>
                run(
                  "toggle",
                  () => api("/ai/assistant", { method: "PATCH", body: { auto_reply_enabled: !assistant.auto_reply_enabled } }),
                  assistant.auto_reply_enabled ? "Automatic replies are off." : "Automatic replies are on.",
                )
              }
            >
              {assistant.auto_reply_enabled ? "Turn off" : "Turn on automatic replies"}
            </Button>
          </div>
        )}
      </header>
      {error && <Alert tone="error">{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}
      {assistant && assistant.warnings.length > 0 && (
        <Alert tone="warning">
          <ul className="list-disc space-y-1 pl-4">
            {assistant.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </Alert>
      )}

      <ProvidersCard providers={data.providers} busy={busy} run={run} />

      {data.models.length === 0 ? (
        <Card title="Assistant">
          <EmptyState
            title="No AI models are available yet"
            description="XploSys turns models on in the admin console. Please contact support if this persists."
          />
        </Card>
      ) : (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
          <AssistantForm
            draft={draft}
            setDraft={setDraft}
            models={data.models}
            saving={busy === "save"}
            onSave={() =>
              run("save", () => api("/ai/assistant", { method: "PUT", body: { config: draft } }), "Saved as a new version.", true)
            }
          />
          <PreviewCard draft={draft} />
        </div>
      )}

      {data.versions.length > 0 && (
        <Card title="Version history" description="Every save is kept. Activate an earlier version to roll back.">
          <ul className="divide-y divide-line">
            {data.versions.map((v) => {
              const active = assistant?.active_version?.version === v.version;
              return (
                <li key={v.version} className="flex flex-wrap items-center justify-between gap-3 py-3">
                  <div>
                    <p className="text-sm font-medium">
                      Version {v.version} {active && <Badge tone="brand">Active</Badge>}
                    </p>
                    <p className="text-xs text-muted">
                      {formatDateTime(v.created_at)} · {PROVIDER_LABEL[v.config.primary.provider]} {v.config.primary.model}
                      {v.config.fallbacks.length > 0 && ` + ${v.config.fallbacks.length} fallback`}
                    </p>
                  </div>
                  {!active && (
                    <Button
                      variant="secondary"
                      className="py-1"
                      loading={busy === `v${v.version}`}
                      onClick={() =>
                        run(`v${v.version}`, () => api(`/ai/assistant/versions/${v.version}/activate`, { method: "POST" }), `Version ${v.version} is active.`, true)
                      }
                    >
                      Activate
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        </Card>
      )}

      <UsageCard usage={data.usage} />
    </div>
  );
}

function ProvidersCard({
  providers,
  busy,
  run,
}: {
  providers: AIProvider[];
  busy: string | null;
  run: (key: string, fn: () => Promise<unknown>, success: string | null) => Promise<void>;
}) {
  return (
    <Card
      title="AI providers"
      description="Add an API key from the provider's website. Keys are encrypted and never shown again. You pay the provider directly for what you use."
    >
      <div className="grid gap-4 lg:grid-cols-3">
        {providers.map((p) => (
          <ProviderPanel key={p.provider} provider={p} busy={busy} run={run} />
        ))}
      </div>
    </Card>
  );
}

function ProviderPanel({
  provider: p,
  busy,
  run,
}: {
  provider: AIProvider;
  busy: string | null;
  run: (key: string, fn: () => Promise<unknown>, success: string | null) => Promise<void>;
}) {
  const [keySource, setKeySource] = useState(p.key_source);
  const [apiKey, setApiKey] = useState("");
  const [enabled, setEnabled] = useState(p.enabled);
  const status = p.ready ? { tone: "success" as const, label: "Ready" } : p.enabled ? { tone: "warning" as const, label: "Not usable" } : { tone: "neutral" as const, label: "Off" };

  return (
    <form
      className="space-y-3 rounded-control border border-line p-4"
      onSubmit={(e) => {
        e.preventDefault();
        const body: Record<string, unknown> = { enabled, key_source: keySource };
        if (apiKey.trim()) body.api_key = apiKey.trim();
        run(`save-${p.provider}`, () => api(`/ai/providers/${p.provider}`, { method: "PUT", body }), `${p.label} settings saved.`).then(() =>
          setApiKey(""),
        );
      }}
    >
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">{p.label}</h3>
        <Badge tone={status.tone}>{status.label}</Badge>
      </div>
      {p.platform_key_available && (
        <SelectField
          label="Key"
          value={keySource}
          onChange={(e) => setKeySource(e.target.value as "tenant" | "platform")}
          options={[
            { value: "tenant", label: "My own API key" },
            { value: "platform", label: "XploSys key (included in plan)" },
          ]}
        />
      )}
      {keySource === "tenant" && (
        <TextField
          label="API key"
          type="password"
          autoComplete="off"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={p.has_own_key ? `Saved key ending ${p.key_hint ?? "••••"}` : "Paste your key"}
        />
      )}
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="h-4 w-4 accent-[var(--xp-brand)]" />
        Use {p.label} for replies
      </label>
      {p.last_test_at && (
        <p className={`text-xs ${p.last_test_ok ? "text-success" : "text-danger"}`}>
          {p.last_test_ok ? "Key works" : `Key check failed: ${p.last_error ?? "unknown error"}`} · {formatDateTime(p.last_test_at)}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" className="py-1.5" loading={busy === `save-${p.provider}`}>
          Save
        </Button>
        {(p.has_own_key || p.key_source === "platform") && (
          <Button
            type="button"
            variant="secondary"
            className="py-1.5"
            loading={busy === `test-${p.provider}`}
            onClick={() => run(`test-${p.provider}`, () => api(`/ai/providers/${p.provider}/test`, { method: "POST" }), null)}
          >
            Check key
          </Button>
        )}
        {p.has_own_key && (
          <Button
            type="button"
            variant="ghost"
            className="py-1.5"
            loading={busy === `del-${p.provider}`}
            onClick={() => {
              if (window.confirm(`Delete the saved ${p.label} key?`))
                run(`del-${p.provider}`, () => api(`/ai/providers/${p.provider}/key`, { method: "DELETE" }), "Key deleted.");
            }}
          >
            Delete key
          </Button>
        )}
      </div>
    </form>
  );
}

function ModelSelect({
  label,
  value,
  models,
  onChange,
}: {
  label: string;
  value: ModelChoice;
  models: AIModel[];
  onChange: (v: ModelChoice) => void;
}) {
  const key = `${value.provider}|${value.model}`;
  const known = models.some((m) => `${m.provider}|${m.model_id}` === key);
  return (
    <SelectField
      label={label}
      value={key}
      onChange={(e) => {
        const [provider, model] = e.target.value.split("|");
        onChange({ provider: provider as ProviderKey, model });
      }}
      options={[
        ...(known ? [] : [{ value: key, label: `${value.model || "Choose a model"} (not available)` }]),
        ...models.map((m) => ({ value: `${m.provider}|${m.model_id}`, label: `${PROVIDER_LABEL[m.provider]} · ${m.display_name ?? m.model_id}` })),
      ]}
    />
  );
}

function AssistantForm({
  draft,
  setDraft,
  models,
  saving,
  onSave,
}: {
  draft: AssistantConfig;
  setDraft: (c: AssistantConfig) => void;
  models: AIModel[];
  saving: boolean;
  onSave: () => void;
}) {
  const set = <K extends keyof AssistantConfig>(key: K, value: AssistantConfig[K]) => setDraft({ ...draft, [key]: value });
  return (
    <Card title="Assistant settings" description="Saving creates a new version and makes it active.">
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          onSave();
        }}
      >
        <div className="grid gap-4 md:grid-cols-2">
          <TextField label="Assistant name" value={draft.assistant_name} maxLength={60} onChange={(e) => set("assistant_name", e.target.value)} required />
          <SelectField
            label="Tone"
            value={draft.tone}
            onChange={(e) => set("tone", e.target.value as AssistantConfig["tone"])}
            options={[
              { value: "friendly", label: "Friendly" },
              { value: "professional", label: "Professional" },
              { value: "concise", label: "Short and to the point" },
            ]}
          />
        </div>
        <TextAreaField
          label="About your business"
          value={draft.business_description}
          maxLength={4000}
          onChange={(e) => set("business_description", e.target.value)}
          placeholder="Sunrise Dental is a family dental clinic in Pune…"
        />
        <TextAreaField
          label="Facts the assistant may use"
          hint="Services, prices, address, phone, policies, FAQs. Replies that mention a price, time, phone number or link that isn't written here or said by the customer are never sent."
          value={draft.instructions}
          maxLength={20000}
          rows={8}
          onChange={(e) => set("instructions", e.target.value)}
        />
        <TextAreaField label="Opening hours" value={draft.business_hours} maxLength={1000} rows={2} onChange={(e) => set("business_hours", e.target.value)} />
        <div className="grid gap-4 md:grid-cols-2">
          <SelectField
            label="Reply language"
            value={draft.reply_language}
            onChange={(e) => set("reply_language", e.target.value as AssistantConfig["reply_language"])}
            options={[
              { value: "match_customer", label: "Same as the customer" },
              { value: "business_default", label: "Business default language" },
            ]}
          />
          <TextField
            label="Longest reply (characters)"
            type="number"
            min={100}
            max={4096}
            value={draft.max_reply_chars}
            onChange={(e) => set("max_reply_chars", Number(e.target.value))}
          />
        </div>
        <TextAreaField
          label="Handover message"
          hint="Sent when the assistant can't answer or a customer needs a person. Automatic replies then stop for that conversation until your team turns them back on."
          value={draft.handover_message}
          maxLength={1000}
          rows={2}
          onChange={(e) => set("handover_message", e.target.value)}
          required
        />
        <div className="space-y-3 rounded-control bg-surface-muted p-4">
          <ModelSelect label="Main model" value={draft.primary} models={models} onChange={(v) => set("primary", v)} />
          {draft.fallbacks.map((f, i) => (
            <div key={i} className="flex items-end gap-2">
              <div className="flex-1">
                <ModelSelect
                  label={`Fallback ${i + 1}`}
                  value={f}
                  models={models}
                  onChange={(v) => set("fallbacks", draft.fallbacks.map((x, j) => (j === i ? v : x)))}
                />
              </div>
              <Button type="button" variant="ghost" onClick={() => set("fallbacks", draft.fallbacks.filter((_, j) => j !== i))}>
                Remove
              </Button>
            </div>
          ))}
          {draft.fallbacks.length < 3 && models.length > 1 && (
            <Button
              type="button"
              variant="secondary"
              className="py-1.5"
              onClick={() => {
                const used = new Set([draft.primary, ...draft.fallbacks].map((c) => `${c.provider}|${c.model}`));
                const next = models.find((m) => !used.has(`${m.provider}|${m.model_id}`)) ?? models[0];
                set("fallbacks", [...draft.fallbacks, { provider: next.provider, model: next.model_id }]);
              }}
            >
              Add a fallback model
            </Button>
          )}
          <p className="text-xs text-muted">If a model fails or gives an answer that doesn&apos;t pass the checks, the next one is tried.</p>
        </div>
        <Button type="submit" loading={saving}>
          Save new version
        </Button>
      </form>
    </Card>
  );
}

function PreviewCard({ draft }: { draft: AssistantConfig }) {
  const [turns, setTurns] = useState<{ role: "user" | "assistant"; text: string }[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState<PreviewResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    const messages = [...turns, { role: "user" as const, text }];
    setBusy(true);
    setError(null);
    try {
      const r = await api<PreviewResult>("/ai/assistant/preview", { method: "POST", body: { messages, config: draft } });
      setTurns([...messages, { role: "assistant", text: r.text }]);
      setLast(r);
      setText("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title="Try it"
      description="Chat with the assistant using the settings on the left, saved or not. Nothing is sent on WhatsApp. Uses your AI provider."
      actions={
        turns.length > 0 && (
          <Button variant="ghost" className="py-1" onClick={() => { setTurns([]); setLast(null); }}>
            Clear
          </Button>
        )
      }
    >
      <div className="mb-4 max-h-[420px] space-y-2 overflow-y-auto">
        {turns.length === 0 && <p className="text-sm text-muted">Ask a question a customer might send.</p>}
        {turns.map((t, i) => (
          <div key={i} className={`flex ${t.role === "user" ? "justify-end" : "justify-start"}`}>
            <p className={`max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${t.role === "user" ? "bg-brand-soft text-brand-ink" : "bg-surface-muted"}`}>
              {t.text}
            </p>
          </div>
        ))}
      </div>
      {last && (
        <div className="mb-4 space-y-1 text-xs text-muted">
          <p>
            {last.kind === "handover" ? (
              <Badge tone="warning">Handed over to your team</Badge>
            ) : last.out_of_scope ? (
              <Badge tone="neutral">Off-topic question declined</Badge>
            ) : (
              <Badge tone="success">Answered</Badge>
            )}{" "}
            {last.provider && `${PROVIDER_LABEL[last.provider]} · ${last.model}`}
          </p>
          {last.attempts
            .filter((a) => a.outcome !== "ok")
            .map((a, i) => (
              <p key={i}>
                {PROVIDER_LABEL[a.provider]} {a.model}: {OUTCOME_LABEL[a.outcome] ?? a.outcome}
                {a.detail ? ` (${a.detail})` : ""}
              </p>
            ))}
        </div>
      )}
      {error && <p className="mb-2 text-sm text-danger">{error}</p>}
      <form onSubmit={ask} className="flex items-end gap-2">
        <div className="flex-1">
          <TextField label="Customer message" value={text} onChange={(e) => setText(e.target.value)} maxLength={4096} required />
        </div>
        <Button type="submit" loading={busy}>
          Ask
        </Button>
      </form>
    </Card>
  );
}

function UsageCard({ usage }: { usage: AIUsage }) {
  const month = new Date(usage.period_start).toLocaleDateString(undefined, { month: "long", year: "numeric" });
  return (
    <Card title={`Usage in ${month}`} description="Token counts come from the providers. Costs are estimates from published prices; your provider's bill is final.">
      <p className="mb-4 text-sm">
        <span className="font-semibold">{usage.ai_replies_used}</span> of {usage.ai_replies_limit} AI replies in your plan this month.
      </p>
      {usage.rows.length === 0 ? (
        <p className="text-sm text-muted">No AI requests yet this month.</p>
      ) : (
        <div className="-mx-6 overflow-x-auto">
          <table className="w-full min-w-[620px] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-muted">
                <th className="px-6 py-2 font-medium">Model</th>
                <th className="px-3 py-2 font-medium">Requests</th>
                <th className="px-3 py-2 font-medium">Tokens in / out</th>
                <th className="px-6 py-2 font-medium">Estimated cost</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {usage.rows.map((r) => (
                <tr key={`${r.provider}|${r.model}`}>
                  <td className="px-6 py-2">
                    {PROVIDER_LABEL[r.provider] ?? r.provider} · {r.model}
                  </td>
                  <td className="px-3 py-2">
                    {r.calls}
                    {r.failed_calls > 0 && <span className="text-muted"> ({r.failed_calls} failed)</span>}
                  </td>
                  <td className="px-3 py-2">
                    {r.input_tokens.toLocaleString()} / {r.output_tokens.toLocaleString()}
                  </td>
                  <td className="px-6 py-2">
                    {r.cost_estimate !== null && r.currency ? `${Number(r.cost_estimate).toFixed(4)} ${r.currency}` : "No price on file"}
                    {r.unpriced_calls > 0 && r.cost_estimate !== null && <span className="text-muted"> (+{r.unpriced_calls} unpriced)</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
