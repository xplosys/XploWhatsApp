"use client";

import { useCallback, useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type { AdminAIModel, AIPrice, ProviderKey } from "@/lib/api/types";
import { formatDateTime } from "@/lib/locale";

const PROVIDERS: { value: ProviderKey; label: string }[] = [
  { value: "gemini", label: "Google Gemini" },
  { value: "openai", label: "OpenAI" },
  { value: "deepseek", label: "DeepSeek" },
];

/** The platform-wide model catalogue: which models businesses may pick, and their dated prices. */
export function AICatalogueCard() {
  const [provider, setProvider] = useState<ProviderKey>("gemini");
  const [models, setModels] = useState<AdminAIModel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [openPrice, setOpenPrice] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(
    () =>
      api<AdminAIModel[]>(`/admin/ai/models?provider=${provider}`).then(
        (list) => {
          setModels(list);
          setError(null);
        },
        (err) => setError(errorMessage(err)),
      ),
    [provider],
  );

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  async function run(key: string, action: () => Promise<unknown>, done?: string) {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await action();
      if (done) setNotice(done);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function sync() {
    setBusy("sync");
    setError(null);
    setNotice(null);
    try {
      const r = await api<{ added: number; updated: number }>("/admin/ai/models/sync", {
        method: "POST",
        body: { provider },
      });
      setNotice(`Synced: ${r.added} new (inactive until you turn them on), ${r.updated} updated.`);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card
      title="AI model catalogue"
      actions={
        <div className="flex flex-wrap items-end gap-2">
          <SelectField
            label="Provider"
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value as ProviderKey);
              setOpenPrice(null);
            }}
            options={PROVIDERS}
          />
          <Button variant="secondary" loading={busy === "sync"} onClick={sync}>
            Sync from provider
          </Button>
          <Button variant="ghost" onClick={() => setAdding((v) => !v)}>
            {adding ? "Cancel" : "Add model"}
          </Button>
        </div>
      }
    >
      <p className="mb-4 text-sm text-muted">
        Businesses can only choose active models. Sync uses the platform API key for this provider. Prices are never edited:
        a new price takes effect from its date, so past cost estimates stay as they were.
      </p>
      {error && <Alert tone="error">{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}
      {adding && (
        <AddModelForm
          provider={provider}
          onDone={() => {
            setAdding(false);
            void load();
          }}
        />
      )}
      {models === null && !error ? (
        <PageLoader />
      ) : models && models.length === 0 ? (
        <EmptyState title="No models for this provider" description="Sync from the provider or add a model by hand." />
      ) : (
        models && (
          <div className="-mx-6 overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-muted">
                  <th className="px-6 py-2 font-medium">Model</th>
                  <th className="px-3 py-2 font-medium">Price per million tokens</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-6 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {models.map((m) => (
                  <ModelRow
                    key={m.id}
                    model={m}
                    busy={busy === m.id}
                    priceOpen={openPrice === m.id}
                    onTogglePrice={() => setOpenPrice((cur) => (cur === m.id ? null : m.id))}
                    onToggleActive={() =>
                      run(m.id, () => api(`/admin/ai/models/${m.id}`, { method: "PATCH", body: { active: !m.active } }))
                    }
                    onPriced={() => {
                      setOpenPrice(null);
                      setNotice(`New price saved for ${m.model_id}.`);
                      void load();
                    }}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </Card>
  );
}

function priceText(p: AIPrice | null): string {
  if (!p) return "No price";
  const cached = p.cached_input_per_mtok !== null ? ` · cached ${p.cached_input_per_mtok}` : "";
  return `in ${p.input_per_mtok} · out ${p.output_per_mtok}${cached} ${p.currency}`;
}

function ModelRow({
  model: m,
  busy,
  priceOpen,
  onTogglePrice,
  onToggleActive,
  onPriced,
}: {
  model: AdminAIModel;
  busy: boolean;
  priceOpen: boolean;
  onTogglePrice: () => void;
  onToggleActive: () => void;
  onPriced: () => void;
}) {
  return (
    <>
      <tr>
        <td className="px-6 py-3">
          <p className="font-medium">
            {m.display_name || m.model_id} {m.kind === "embedding" && <Badge tone="brand">Embedding</Badge>}
          </p>
          <p className="text-xs text-muted">
            {m.model_id}
            {m.last_seen_at ? ` · last seen ${formatDateTime(m.last_seen_at)}` : " · added by hand"}
          </p>
        </td>
        <td className="px-3 py-3">
          <p>{priceText(m.current_price)}</p>
          {m.current_price && <p className="text-xs text-muted">since {formatDateTime(m.current_price.effective_from)}</p>}
        </td>
        <td className="px-3 py-3">
          <Badge tone={m.active ? "success" : "neutral"}>{m.active ? "Active" : "Inactive"}</Badge>
        </td>
        <td className="space-x-2 whitespace-nowrap px-6 py-3 text-right">
          <Button variant="ghost" className="py-1" onClick={onTogglePrice}>
            {priceOpen ? "Close" : "Prices"}
          </Button>
          <Button variant={m.active ? "secondary" : "primary"} className="py-1" loading={busy} onClick={onToggleActive}>
            {m.active ? "Deactivate" : "Activate"}
          </Button>
        </td>
      </tr>
      {priceOpen && (
        <tr>
          <td colSpan={4} className="bg-surface-muted px-6 py-4">
            <PricePanel model={m} onSaved={onPriced} />
          </td>
        </tr>
      )}
    </>
  );
}

function PricePanel({ model, onSaved }: { model: AdminAIModel; onSaved: () => void }) {
  const [history, setHistory] = useState<AIPrice[] | null>(null);
  const [input, setInput] = useState("");
  const [output, setOutput] = useState("");
  const [cached, setCached] = useState("");
  const [currency, setCurrency] = useState(model.current_price?.currency ?? "USD");
  const [from, setFrom] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<AIPrice[]>(`/admin/ai/models/${model.id}/prices`).then(setHistory, (err) => setError(errorMessage(err)));
  }, [model.id]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(`/admin/ai/models/${model.id}/prices`, {
        method: "POST",
        body: {
          input_per_mtok: input,
          output_per_mtok: output,
          cached_input_per_mtok: cached || null,
          currency: currency.toUpperCase(),
          effective_from: from ? new Date(from).toISOString() : null,
          source_note: note || null,
        },
      });
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h4 className="text-sm font-semibold">Price history</h4>
        {history === null && !error ? (
          <p className="text-xs text-muted">Loading…</p>
        ) : history && history.length === 0 ? (
          <p className="text-xs text-muted">No prices yet. Calls to this model are recorded as unpriced.</p>
        ) : (
          <ul className="mt-1 space-y-0.5 text-xs">
            {history?.map((p) => (
              <li key={p.id}>
                {formatDateTime(p.effective_from)}: {priceText(p)}
                {p.source_note ? ` (${p.source_note})` : ""}
              </li>
            ))}
          </ul>
        )}
      </div>
      <form onSubmit={save} className="space-y-3">
        <h4 className="text-sm font-semibold">Add a price</h4>
        <div className="grid gap-3 md:grid-cols-3">
          <TextField label="Input" inputMode="decimal" value={input} onChange={(e) => setInput(e.target.value)} required />
          <TextField label="Output" inputMode="decimal" value={output} onChange={(e) => setOutput(e.target.value)} required />
          <TextField label="Cached input (optional)" inputMode="decimal" value={cached} onChange={(e) => setCached(e.target.value)} />
          <TextField label="Currency" value={currency} onChange={(e) => setCurrency(e.target.value)} maxLength={3} required />
          <TextField
            label="Effective from (blank means now)"
            type="datetime-local"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
          />
          <TextField label="Source (optional)" value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} placeholder="Pricing page, date checked" />
        </div>
        <div className="flex items-center gap-3">
          <Button type="submit" loading={busy}>
            Save price
          </Button>
          {error && <span className="text-sm text-danger">{error}</span>}
        </div>
      </form>
    </div>
  );
}

function AddModelForm({ provider, onDone }: { provider: ProviderKey; onDone: () => void }) {
  const [modelId, setModelId] = useState("");
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"chat" | "embedding">("chat");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/admin/ai/models", {
        method: "POST",
        body: { provider, model_id: modelId.trim(), display_name: name.trim() || null, kind, active: false },
      });
      onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={add} className="mb-4 space-y-3 rounded-control border border-line p-4">
      <div className="grid gap-3 md:grid-cols-2">
        <TextField
          label="Model ID, exactly as the provider names it"
          value={modelId}
          onChange={(e) => setModelId(e.target.value)}
          pattern="[A-Za-z0-9._:/\-]+"
          required
        />
        <TextField label="Display name (optional)" value={name} onChange={(e) => setName(e.target.value)} />
        <SelectField
          label="Used for"
          value={kind}
          onChange={(e) => setKind(e.target.value as "chat" | "embedding")}
          options={[
            { value: "chat", label: "Answering customers (chat)" },
            { value: "embedding", label: "Knowledge search (embeddings, 1536 dimensions)" },
          ]}
        />
      </div>
      <p className="text-xs text-muted">New models start inactive. Add a price, then activate it.</p>
      <div className="flex items-center gap-3">
        <Button type="submit" loading={busy}>
          Add model
        </Button>
        {error && <span className="text-sm text-danger">{error}</span>}
      </div>
    </form>
  );
}
