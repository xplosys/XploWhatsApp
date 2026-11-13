"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField, TextAreaField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, apiUpload, errorMessage } from "@/lib/api/client";
import type {
  EmbeddingModel,
  KnowledgeChunk,
  KnowledgeDocument,
  KnowledgeDocumentDetail,
  KnowledgeSearch,
  KnowledgeSettings,
  KnowledgeVersion,
  VersionStatus,
} from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatDateTime } from "@/lib/locale";

const PROVIDER_LABEL: Record<string, string> = { gemini: "Google Gemini", openai: "OpenAI", deepseek: "DeepSeek" };

const STATUS: Record<VersionStatus, { label: string; tone: "neutral" | "brand" | "success" | "warning" | "danger" }> = {
  queued: { label: "Waiting", tone: "neutral" },
  processing: { label: "Reading", tone: "brand" },
  ready: { label: "Ready", tone: "success" },
  replaced: { label: "Replaced", tone: "neutral" },
  failed: { label: "Failed", tone: "danger" },
  rejected: { label: "Rejected", tone: "danger" },
};

const SOURCE_LABEL = { file: "File", url: "Web page", text: "Note" } as const;
const ACCEPT = ".pdf,.docx,.xlsx,.csv,.txt,.md,.markdown";

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function modelKey(m: EmbeddingModel | null): string {
  return m ? `${m.provider}:${m.model_id}` : "";
}

function modelLabel(m: EmbeddingModel): string {
  return `${PROVIDER_LABEL[m.provider] ?? m.provider} · ${m.display_name || m.model_id}`;
}

function busyStatus(s: VersionStatus | undefined) {
  return s === "queued" || s === "processing";
}

function where(meta: KnowledgeChunk["meta"]): string | null {
  if (meta.pages?.length) return meta.pages.length === 1 ? `Page ${meta.pages[0]}` : `Pages ${meta.pages[0]}-${meta.pages.at(-1)}`;
  if (meta.sheet) return meta.rows ? `${meta.sheet}, rows ${meta.rows}` : meta.sheet;
  return null;
}

interface Data {
  settings: KnowledgeSettings;
  models: EmbeddingModel[];
  documents: KnowledgeDocument[];
}

async function fetchAll(): Promise<Data> {
  const [settings, models, documents] = await Promise.all([
    api<KnowledgeSettings>("/knowledge/settings"),
    api<EmbeddingModel[]>("/knowledge/embedding-models"),
    api<KnowledgeDocument[]>("/knowledge/documents"),
  ]);
  return { settings, models, documents };
}

export default function KnowledgePage() {
  const { can } = useAuth();
  const allowed = can("knowledge:read");
  const manage = can("knowledge:manage");
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [version, setVersion] = useState(0); // bumps when documents change, so the open panel reloads

  const reload = useCallback(
    () =>
      fetchAll().then(
        (d) => {
          setData(d);
          setVersion((v) => v + 1);
        },
        (err) => setError(errorMessage(err)),
      ),
    [],
  );

  useEffect(() => {
    if (allowed) void reload();
  }, [allowed, reload]);

  // While anything is being read or re-indexed, check again every few seconds.
  const working =
    !!data &&
    (data.documents.some((d) => busyStatus(d.latest?.status)) ||
      (!!data.settings.pending_embedding_model && !data.settings.reindex_error));
  useEffect(() => {
    if (!working) return;
    const timer = setInterval(() => void reload(), 3000);
    return () => clearInterval(timer);
  }, [working, reload]);

  if (!allowed) return <Alert tone="error">You don&apos;t have access to the knowledge base.</Alert>;
  if (!data) return error ? <Alert tone="error">{error}</Alert> : <PageLoader />;

  const { settings } = data;
  const ready = !!settings.embedding_model && settings.storage_ready;
  const say = (msg: string | null, err: string | null = null) => {
    setNotice(msg);
    setError(err);
  };

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Knowledge base</h1>
          <p className="mt-1 text-sm text-muted">
            Documents, web pages and notes the AI assistant reads before answering. It quotes prices and facts only from here and
            your assistant settings.
          </p>
        </div>
        <div className="text-sm text-muted sm:text-right">
          <p>
            <span className="font-semibold text-ink">{settings.usage.documents}</span> of {settings.usage.max_documents} documents
          </p>
          <p>
            {bytes(settings.usage.storage_bytes)} of {bytes(settings.usage.max_storage_bytes)} stored
          </p>
        </div>
      </header>
      {error && <Alert tone="error">{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}
      {!settings.storage_ready && (
        <Alert tone="warning">File storage is not configured on this server, so documents can&apos;t be added yet. Please contact support.</Alert>
      )}
      {!settings.virus_scan && (
        <Alert tone="info">Virus scanning is off on this development server. Uploads are still checked for type and size.</Alert>
      )}

      {manage && <SettingsCard data={data} onSaved={reload} say={say} />}
      {manage && ready && <AddCard settings={settings} onAdded={reload} say={say} />}

      <Card title="Documents" description="Each change makes a new version. Answers use the newest version that is ready.">
        {data.documents.length === 0 ? (
          <EmptyState
            title="No documents yet"
            description={
              manage
                ? ready
                  ? "Add a price list, a brochure, your website's pages or a note above."
                  : "Choose an embedding model first, then add documents."
                : "An owner or admin can add documents."
            }
          />
        ) : (
          <ul className="divide-y divide-line">
            {data.documents.map((d) => (
              <li key={d.id} className="py-3">
                <button
                  type="button"
                  className="flex w-full flex-wrap items-center justify-between gap-3 text-left"
                  onClick={() => setOpen(open === d.id ? null : d.id)}
                  aria-expanded={open === d.id}
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{d.title}</p>
                    <p className="truncate text-xs text-muted">
                      {SOURCE_LABEL[d.source_type]}
                      {d.source_url ? ` · ${d.source_url}` : ""}
                      {d.latest ? ` · version ${d.latest.version}` : ""}
                      {d.latest?.status === "ready" ? ` · ${d.latest.chunk_count} ${d.latest.chunk_count === 1 ? "passage" : "passages"}` : ""}
                      {` · updated ${formatDateTime(d.updated_at)}`}
                    </p>
                    {d.latest && (d.latest.status === "failed" || d.latest.status === "rejected") && (
                      <p className="mt-1 text-xs text-danger">{d.latest.error}</p>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    {d.latest && <Badge tone={STATUS[d.latest.status].tone}>{STATUS[d.latest.status].label}</Badge>}
                    {d.active_version && d.latest && d.latest.version !== d.active_version && (
                      <Badge tone="neutral">Using v{d.active_version}</Badge>
                    )}
                    <span className="text-xs text-muted">{open === d.id ? "Hide" : "Details"}</span>
                  </div>
                </button>
                {open === d.id && (
                  <DocumentPanel
                    id={d.id}
                    refreshKey={version}
                    manage={manage}
                    onChanged={reload}
                    onDeleted={() => {
                      setOpen(null);
                      void reload();
                    }}
                    say={say}
                  />
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {manage && settings.embedding_model && <SearchCard minScore={Number(settings.min_score)} />}
    </div>
  );
}

type Say = (notice: string | null, error?: string | null) => void;

function SettingsCard({ data, onSaved, say }: { data: Data; onSaved: () => Promise<void>; say: Say }) {
  const { settings, models } = data;
  const [model, setModel] = useState(modelKey(settings.pending_embedding_model ?? settings.embedding_model) || modelKey(models[0] ?? null));
  const [minScore, setMinScore] = useState(settings.min_score);
  const [topK, setTopK] = useState(String(settings.top_k));
  const [busy, setBusy] = useState<string | null>(null);

  const current = modelKey(settings.embedding_model);
  const changing = !!current && model !== current && settings.usage.documents > 0;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy("save");
    try {
      const [provider, ...rest] = model.split(":");
      const body: Record<string, unknown> = { min_score: minScore, top_k: Number(topK) };
      if (model && model !== modelKey(settings.pending_embedding_model ?? settings.embedding_model)) {
        body.embedding_provider = provider;
        body.embedding_model = rest.join(":");
      }
      await api("/knowledge/settings", { method: "PUT", body });
      say(changing ? "Saved. Your documents are being re-indexed for the new model." : "Knowledge settings saved.");
      await onSaved();
    } catch (err) {
      say(null, errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function retryReindex() {
    setBusy("reindex");
    try {
      await api("/knowledge/settings/reindex", { method: "POST" });
      say("Re-indexing started again.");
      await onSaved();
    } catch (err) {
      say(null, errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  if (models.length === 0) {
    return (
      <Card title="Search settings">
        <EmptyState
          title="No embedding models are available yet"
          description="XploSys turns embedding models on in the admin console. Please contact support if this persists."
        />
      </Card>
    );
  }

  const pending = settings.pending_embedding_model;
  return (
    <Card
      title="Search settings"
      description="The embedding model turns your documents and each customer question into vectors so the closest passages can be found. It uses the provider key from the AI assistant page."
    >
      {!settings.embedding_model && (
        <div className="mb-4">
          <Alert tone="info">Choose an embedding model and save to start adding documents.</Alert>
        </div>
      )}
      {pending && (
        <div className="mb-4">
          <Alert tone={settings.reindex_error ? "error" : "info"}>
            <p>
              Switching to {modelLabel(pending)}: {settings.reindex_done} of {settings.reindex_total} passages re-indexed. Answers keep
              using {settings.embedding_model ? modelLabel(settings.embedding_model) : "the current model"} until it finishes.
            </p>
            {settings.reindex_error && (
              <div className="mt-2 flex flex-wrap items-center gap-3">
                <span>{settings.reindex_error}</span>
                <Button variant="secondary" className="py-1" loading={busy === "reindex"} onClick={retryReindex}>
                  Try again
                </Button>
              </div>
            )}
          </Alert>
        </div>
      )}
      <form onSubmit={save} className="space-y-4">
        <div className="grid gap-4 md:grid-cols-3">
          <SelectField
            label="Embedding model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            options={models.map((m) => ({ value: modelKey(m), label: modelLabel(m) }))}
          />
          <TextField
            label="Minimum match score"
            type="number"
            min={0}
            max={1}
            step={0.01}
            value={minScore}
            onChange={(e) => setMinScore(e.target.value)}
            hint="0 to 1. Passages below this are not given to the assistant. Use the search test below to tune it."
            required
          />
          <TextField
            label="Passages per answer"
            type="number"
            min={1}
            max={20}
            value={topK}
            onChange={(e) => setTopK(e.target.value)}
            hint="The closest passages above the score, up to this many."
            required
          />
        </div>
        {changing && (
          <p className="text-sm text-warning">
            Changing the model re-indexes all {settings.usage.documents} documents with it. This uses your provider&apos;s
            embedding API.
          </p>
        )}
        <Button type="submit" loading={busy === "save"}>
          Save settings
        </Button>
      </form>
    </Card>
  );
}

function AddCard({ settings, onAdded, say }: { settings: KnowledgeSettings; onAdded: () => Promise<void>; say: Say }) {
  const [tab, setTab] = useState<"file" | "url" | "text">("file");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      if (tab === "file") {
        const file = fileRef.current?.files?.[0];
        if (!file) throw new Error("Choose a file.");
        if (file.size > settings.max_file_mb * 1024 * 1024) {
          say(null, `Files can be up to ${settings.max_file_mb} MB.`);
          return;
        }
        const form = new FormData();
        form.append("file", file);
        if (title.trim()) form.append("title", title.trim());
        await apiUpload("/knowledge/documents/upload", form);
        if (fileRef.current) fileRef.current.value = "";
      } else if (tab === "url") {
        await api("/knowledge/documents/url", { method: "POST", body: { url: url.trim(), title: title.trim() || null } });
        setUrl("");
      } else {
        await api("/knowledge/documents/text", { method: "POST", body: { title: title.trim(), text } });
        setText("");
      }
      setTitle("");
      say("Added. It will be ready in a moment.");
      await onAdded();
    } catch (err) {
      say(null, err instanceof Error && !("status" in err) ? err.message : errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const tabs = [
    { key: "file", label: "Upload a file" },
    { key: "url", label: "Add a web page" },
    { key: "text", label: "Write a note" },
  ] as const;

  return (
    <Card title="Add to the knowledge base">
      <div role="tablist" className="mb-4 flex flex-wrap gap-2">
        {tabs.map((t) => (
          <Button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            variant={tab === t.key ? "primary" : "secondary"}
            className="py-1.5"
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </Button>
        ))}
      </div>
      <form onSubmit={submit} className="space-y-4">
        {tab === "file" && (
          <div className="space-y-1.5">
            <label htmlFor="knowledge-file" className="block text-sm font-medium">
              File
            </label>
            <input
              id="knowledge-file"
              ref={fileRef}
              type="file"
              accept={ACCEPT}
              required
              className="block w-full text-sm file:mr-3 file:rounded-full file:border-0 file:bg-brand-soft file:px-4 file:py-2 file:text-sm file:font-semibold file:text-brand-ink"
            />
            <p className="text-xs text-muted">
              PDF (with selectable text), Word (.docx), Excel (.xlsx), CSV, text or Markdown, up to {settings.max_file_mb} MB. Files with
              macros, password protection or scanned pages can&apos;t be read.
            </p>
          </div>
        )}
        {tab === "url" && (
          <TextField
            label="Web page address"
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://www.example.com/prices"
            hint="A public page on your website. Menus and footers are left out. Fetch it again from its details when it changes."
            maxLength={2000}
            required
          />
        )}
        <TextField
          label={tab === "text" ? "Title" : "Title (optional)"}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          maxLength={200}
          required={tab === "text"}
          placeholder={tab === "file" ? "Uses the file name if empty" : tab === "url" ? "Uses the page title if empty" : "Parking and directions"}
        />
        {tab === "text" && (
          <TextAreaField
            label="Text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={8}
            maxLength={200000}
            hint="Markdown headings (# Heading) keep related facts together."
            required
          />
        )}
        <Button type="submit" loading={busy}>
          Add
        </Button>
      </form>
    </Card>
  );
}

function DocumentPanel({
  id,
  refreshKey,
  manage,
  onChanged,
  onDeleted,
  say,
}: {
  id: string;
  refreshKey: number;
  manage: boolean;
  onChanged: () => Promise<void>;
  onDeleted: () => void;
  say: Say;
}) {
  const [doc, setDoc] = useState<KnowledgeDocumentDetail | null>(null);
  const [chunks, setChunks] = useState<KnowledgeChunk[] | null>(null);
  const [showChunks, setShowChunks] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [editing, setEditing] = useState<{ title: string; text: string } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api<KnowledgeDocumentDetail>(`/knowledge/documents/${id}`).then(setDoc, (err) => say(null, errorMessage(err)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, refreshKey]);

  useEffect(() => {
    if (!showChunks) return;
    api<KnowledgeChunk[]>(`/knowledge/documents/${id}/chunks?limit=200`).then(setChunks, (err) => say(null, errorMessage(err)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showChunks, id, refreshKey]);

  async function act(key: string, fn: () => Promise<unknown>, success: string | null) {
    setBusy(key);
    try {
      await fn();
      if (success) say(success);
      await onChanged();
    } catch (err) {
      say(null, errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function download(v: KnowledgeVersion) {
    setBusy(`dl-${v.version}`);
    try {
      const { url } = await api<{ url: string }>(`/knowledge/documents/${id}/versions/${v.version}/download`);
      window.location.assign(url);
    } catch (err) {
      say(null, errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function replace() {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    await act("replace", () => apiUpload(`/knowledge/documents/${id}/versions`, form), "New version uploaded.");
    if (fileRef.current) fileRef.current.value = "";
  }

  async function startEdit() {
    setBusy("edit");
    try {
      setEditing(await api<{ title: string; text: string }>(`/knowledge/documents/${id}/text`));
    } catch (err) {
      say(null, errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  if (!doc) return <p className="mt-3 text-sm text-muted">Loading…</p>;

  return (
    <div className="mt-3 space-y-4 rounded-control bg-surface-muted p-4">
      {manage && (
        <div className="flex flex-wrap items-center gap-2">
          {doc.source_type === "file" && (
            <>
              <input ref={fileRef} type="file" accept={ACCEPT} className="hidden" onChange={() => void replace()} aria-label="Replacement file" />
              <Button variant="secondary" className="py-1.5" loading={busy === "replace"} onClick={() => fileRef.current?.click()}>
                Upload a new version
              </Button>
            </>
          )}
          {doc.source_type === "url" && (
            <Button
              variant="secondary"
              className="py-1.5"
              loading={busy === "refresh"}
              onClick={() => act("refresh", () => api(`/knowledge/documents/${id}/refresh`, { method: "POST" }), "Fetching the page again.")}
            >
              Fetch again
            </Button>
          )}
          {doc.source_type === "text" && !editing && (
            <Button variant="secondary" className="py-1.5" loading={busy === "edit"} onClick={startEdit}>
              Edit note
            </Button>
          )}
          <Button variant="ghost" className="py-1.5" onClick={() => setShowChunks(!showChunks)}>
            {showChunks ? "Hide passages" : "Show what the assistant reads"}
          </Button>
          {confirmDelete ? (
            <>
              <span className="text-sm text-danger">Delete this document and all its versions?</span>
              <Button
                variant="danger"
                className="py-1.5"
                loading={busy === "delete"}
                onClick={async () => {
                  setBusy("delete");
                  try {
                    await api(`/knowledge/documents/${id}`, { method: "DELETE" });
                    say("Document deleted.");
                    onDeleted();
                  } catch (err) {
                    say(null, errorMessage(err));
                    setBusy(null);
                  }
                }}
              >
                Delete
              </Button>
              <Button variant="ghost" className="py-1.5" onClick={() => setConfirmDelete(false)}>
                Keep
              </Button>
            </>
          ) : (
            <Button variant="danger" className="py-1.5" onClick={() => setConfirmDelete(true)}>
              Delete document
            </Button>
          )}
        </div>
      )}
      {!manage && (
        <Button variant="ghost" className="py-1.5" onClick={() => setShowChunks(!showChunks)}>
          {showChunks ? "Hide passages" : "Show what the assistant reads"}
        </Button>
      )}

      {editing && (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            void act(
              "save-note",
              () => api(`/knowledge/documents/${id}/text`, { method: "PUT", body: editing }),
              "Saved as a new version.",
            ).then(() => setEditing(null));
          }}
        >
          <TextField label="Title" value={editing.title} onChange={(e) => setEditing({ ...editing, title: e.target.value })} maxLength={200} required />
          <TextAreaField label="Text" rows={8} value={editing.text} onChange={(e) => setEditing({ ...editing, text: e.target.value })} maxLength={200000} required />
          <div className="flex gap-2">
            <Button type="submit" loading={busy === "save-note"}>
              Save
            </Button>
            <Button variant="ghost" onClick={() => setEditing(null)}>
              Cancel
            </Button>
          </div>
        </form>
      )}

      <div>
        <h3 className="mb-2 text-sm font-semibold">Versions</h3>
        <ul className="space-y-2">
          {doc.versions.map((v) => (
            <li key={v.version} className="flex flex-wrap items-start justify-between gap-3 rounded-control bg-surface px-3 py-2">
              <div className="min-w-0 text-sm">
                <p className="font-medium">
                  Version {v.version} <Badge tone={STATUS[v.status].tone}>{STATUS[v.status].label}</Badge>{" "}
                  {doc.active_version === v.version && <Badge tone="brand">In use</Badge>}
                </p>
                <p className="text-xs text-muted">
                  {formatDateTime(v.created_at)}
                  {v.original_filename ? ` · ${v.original_filename}` : ""}
                  {v.size_bytes ? ` · ${bytes(v.size_bytes)}` : ""}
                  {v.status === "ready" ? ` · ${v.char_count.toLocaleString()} characters, ${v.chunk_count} ${v.chunk_count === 1 ? "passage" : "passages"}` : ""}
                  {v.scan_result ? ` · virus scan: ${v.scan_result}` : ""}
                </p>
                {v.error && <p className="mt-1 text-xs text-danger">{v.error}</p>}
              </div>
              <div className="flex flex-wrap gap-2">
                {v.downloadable && (
                  <Button variant="ghost" className="py-1" loading={busy === `dl-${v.version}`} onClick={() => download(v)}>
                    Download
                  </Button>
                )}
                {manage && (v.status === "failed" || (v.status === "queued" && !busyStatus(doc.latest?.status))) && (
                  <Button
                    variant="secondary"
                    className="py-1"
                    loading={busy === `retry-${v.version}`}
                    onClick={() =>
                      act(`retry-${v.version}`, () => api(`/knowledge/documents/${id}/versions/${v.version}/retry`, { method: "POST" }), "Trying again.")
                    }
                  >
                    Try again
                  </Button>
                )}
                {manage && v.version !== doc.active_version && !busyStatus(v.status) && (
                  <Button
                    variant="danger"
                    className="py-1"
                    loading={busy === `del-${v.version}`}
                    onClick={() =>
                      act(`del-${v.version}`, () => api(`/knowledge/documents/${id}/versions/${v.version}`, { method: "DELETE" }), `Version ${v.version} deleted.`)
                    }
                  >
                    Delete version
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      </div>

      {showChunks && (
        <div>
          <h3 className="mb-2 text-sm font-semibold">Passages the assistant can read</h3>
          {chunks === null ? (
            <p className="text-sm text-muted">Loading…</p>
          ) : chunks.length === 0 ? (
            <p className="text-sm text-muted">Nothing yet. Passages appear once a version is ready.</p>
          ) : (
            <ol className="max-h-96 space-y-2 overflow-y-auto">
              {chunks.map((c) => (
                <li key={c.ordinal} className="rounded-control bg-surface px-3 py-2 text-sm">
                  <p className="mb-1 text-xs text-muted">
                    #{c.ordinal + 1}
                    {where(c.meta) ? ` · ${where(c.meta)}` : ""}
                  </p>
                  <p className="whitespace-pre-wrap">{c.content}</p>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}

function SearchCard({ minScore }: { minScore: number }) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<KnowledgeSearch | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function search(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResult(await api<KnowledgeSearch>("/knowledge/search", { method: "POST", body: { query } }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const statusText: Record<string, string> = {
    empty: "No documents are ready yet.",
    not_configured: "Choose an embedding model first.",
  };

  return (
    <Card
      title="Test a question"
      description={`See which passages the assistant would get for a customer question. Passages scoring at least ${minScore} are used. Uses your embedding provider.`}
    >
      <form onSubmit={search} className="mb-4 flex items-end gap-2">
        <div className="flex-1">
          <TextField label="Customer question" value={query} onChange={(e) => setQuery(e.target.value)} maxLength={1500} required />
        </div>
        <Button type="submit" loading={busy}>
          Search
        </Button>
      </form>
      {error && <p className="text-sm text-danger">{error}</p>}
      {result && result.status !== "ok" && (
        <p className="text-sm text-muted">{statusText[result.status] ?? `The search failed (${result.status}).`}</p>
      )}
      {result && result.status === "ok" && result.passages.length === 0 && <p className="text-sm text-muted">No passages found.</p>}
      {result && result.passages.length > 0 && (
        <ol className="space-y-2">
          {result.passages.map((p, i) => (
            <li key={i} className={`rounded-control border px-3 py-2 text-sm ${p.used ? "border-ring" : "border-line opacity-70"}`}>
              <p className="mb-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                <span className="font-medium text-ink">{p.title}</span>
                {where(p.meta) && <span>{where(p.meta)}</span>}
                <span>score {p.score.toFixed(3)}</span>
                <Badge tone={p.used ? "success" : "neutral"}>{p.used ? "Used" : "Below threshold"}</Badge>
              </p>
              <p className="whitespace-pre-wrap">{p.content}</p>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
