"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField, TextAreaField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type { Contact, Lead, LeadActivity, LeadDetail, LeadStatus, LeadSummary, Member, Page } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { contactLabel, formatMoney, LEAD_STATUSES, leadStatus } from "@/lib/inbox";
import { formatDate, formatDateTime } from "@/lib/locale";

type StatusFilter = "open" | "closed" | "all" | LeadStatus;

interface Filters {
  status: StatusFilter;
  owner: string;
  q: string;
}

function listUrl(f: Filters): string {
  const p = new URLSearchParams({ limit: "100" });
  if (f.status !== "all") p.set("status", f.status);
  if (f.owner) p.set("owner", f.owner);
  if (f.q.trim().length >= 2) p.set("q", f.q.trim());
  return `/leads?${p}`;
}

export default function LeadsPage() {
  const { can, me } = useAuth();
  const [filters, setFilters] = useState<Filters>({ status: "open", owner: "", q: "" });
  const [search, setSearch] = useState("");
  const [leads, setLeads] = useState<Page<Lead> | null>(null);
  const [summary, setSummary] = useState<LeadSummary | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      Promise.all([api<Page<Lead>>(listUrl(filters)), api<LeadSummary>("/leads/summary")]).then(
        ([page, s]) => {
          setLeads(page);
          setSummary(s);
          setError(null);
        },
        (err) => setError(errorMessage(err)),
      ),
    [filters],
  );

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  useEffect(() => {
    const t = window.setTimeout(() => setFilters((f) => (f.q === search ? f : { ...f, q: search })), 300);
    return () => window.clearTimeout(t);
  }, [search]);

  useEffect(() => {
    if (can("members:read")) {
      api<Page<Member>>("/tenants/current/members?limit=100").then(
        (p) => setMembers(p.items.filter((m) => m.status === "active")),
        () => setMembers([]),
      );
    }
    const wanted = new URLSearchParams(window.location.search).get("lead");
    if (wanted) void Promise.resolve().then(() => setSelectedId(wanted));
  }, [can]);

  if (!leads && !error) return <PageLoader />;

  const ownerOptions = [
    { value: "", label: "Anyone" },
    { value: "me", label: "Me" },
    { value: "none", label: "No owner" },
    ...members.filter((m) => m.user_id !== me?.user.id).map((m) => ({ value: m.user_id, label: m.full_name })),
  ];

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Leads</h1>
          <p className="mt-1 text-sm text-muted">Sales opportunities from your WhatsApp conversations and contacts.</p>
        </div>
        <Button onClick={() => setCreating(true)}>New lead</Button>
      </header>
      {error && <Alert tone="error">{error}</Alert>}

      {summary && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Stat label="Open leads" value={summary.by_status.filter((s) => s.status !== "won" && s.status !== "lost").reduce((a, s) => a + s.count, 0)} />
          <Stat label="Yours, open" value={summary.mine_open} />
          <Stat label="No owner yet" value={summary.unowned_open} />
          <div className="rounded-card border border-line bg-surface p-4 shadow-card">
            <p className="text-xs font-medium text-muted">Open pipeline value</p>
            {summary.open_value.length === 0 ? (
              <p className="mt-1 text-2xl font-bold">—</p>
            ) : (
              summary.open_value.map((v) => (
                <p key={v.currency} className="mt-1 text-xl font-bold tabular-nums">
                  {formatMoney(v.total, v.currency)}
                </p>
              ))
            )}
          </div>
        </div>
      )}

      {creating && (
        <NewLead
          onCancel={() => setCreating(false)}
          onCreated={(lead) => {
            setCreating(false);
            setSelectedId(lead.id);
            void load();
          }}
        />
      )}

      <div className="grid gap-4 xl:grid-cols-[1fr_400px]">
        <Card>
          <div className="mb-4 flex flex-wrap gap-1.5" role="group" aria-label="Lead status">
            {(
              [
                { value: "open", label: "Open" },
                ...LEAD_STATUSES.map((s) => ({ value: s.value, label: s.label })),
                { value: "all", label: "All" },
              ] as { value: StatusFilter; label: string }[]
            ).map((s) => {
              const n = summary?.by_status.find((b) => b.status === s.value)?.count;
              return (
                <button
                  key={s.value}
                  type="button"
                  aria-pressed={filters.status === s.value}
                  onClick={() => setFilters((f) => ({ ...f, status: s.value }))}
                  className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold ${
                    filters.status === s.value ? "bg-brand-soft text-brand-ink" : "text-muted hover:bg-surface-muted hover:text-ink"
                  }`}
                >
                  {s.label}
                  {n !== undefined && <span className="tabular-nums opacity-70">{n}</span>}
                </button>
              );
            })}
          </div>
          <div className="mb-4 grid gap-3 sm:grid-cols-[1fr_200px]">
            <TextField label="Search" placeholder="Title, contact name or phone" value={search} maxLength={100} onChange={(e) => setSearch(e.target.value)} />
            <SelectField label="Owner" value={filters.owner} onChange={(e) => setFilters((f) => ({ ...f, owner: e.target.value }))} options={ownerOptions} />
          </div>
          {leads && leads.items.length === 0 ? (
            <EmptyState
              title={filters.q || filters.owner || filters.status !== "open" ? "No leads match" : "No open leads"}
              description="Create a lead from a conversation's side panel, or with New lead above."
            />
          ) : (
            <div className="-mx-6 overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead className="text-left text-xs text-muted">
                  <tr className="border-b border-line">
                    <th className="px-6 py-2 font-medium">Lead</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 text-right font-medium">Value</th>
                    <th className="px-3 py-2 font-medium">Owner</th>
                    <th className="px-6 py-2 font-medium">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {leads?.items.map((l) => (
                    <tr
                      key={l.id}
                      className={`cursor-pointer border-b border-line last:border-0 ${l.id === selectedId ? "bg-brand-soft" : "hover:bg-surface-muted"}`}
                      onClick={() => setSelectedId(l.id)}
                    >
                      <td className="px-6 py-3">
                        <button type="button" className="text-left font-semibold hover:text-brand" onClick={() => setSelectedId(l.id)}>
                          {l.title}
                        </button>
                        <p className="text-xs text-muted">{l.contact.name ?? l.contact.phone_e164 ?? "WhatsApp user"}</p>
                      </td>
                      <td className="px-3 py-3">
                        <Badge tone={leadStatus(l.status).tone}>{leadStatus(l.status).label}</Badge>
                      </td>
                      <td className="px-3 py-3 text-right tabular-nums">{formatMoney(l.value, l.currency)}</td>
                      <td className="px-3 py-3">{l.owner ? (l.owner.id === me?.user.id ? "You" : l.owner.name) : <span className="text-muted">No owner</span>}</td>
                      <td className="px-6 py-3 text-xs text-muted">{formatDate(l.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {leads && leads.total > leads.items.length && (
                <p className="px-6 pt-3 text-xs text-muted">
                  Showing the {leads.items.length} most recently changed of {leads.total}. Narrow the search to find others.
                </p>
              )}
            </div>
          )}
        </Card>
        {selectedId ? (
          <LeadPanel
            key={selectedId}
            leadId={selectedId}
            members={members}
            onChanged={load}
            onDeleted={() => {
              setSelectedId(null);
              void load();
            }}
          />
        ) : (
          <div className="hidden xl:block">
            <EmptyState title="Select a lead" description="Pick a lead to change its status, value or owner, and to see its history." />
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-card border border-line bg-surface p-4 shadow-card">
      <p className="text-xs font-medium text-muted">{label}</p>
      <p className="mt-1 text-2xl font-bold tabular-nums">{value}</p>
    </div>
  );
}

function NewLead({ onCancel, onCreated }: { onCancel: () => void; onCreated: (lead: LeadDetail) => void }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Contact[]>([]);
  const [contact, setContact] = useState<Contact | null>(null);
  const [title, setTitle] = useState("");
  const [value, setValue] = useState("");
  const [currency, setCurrency] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (contact || q.trim().length < 2) return;
    const t = window.setTimeout(() => {
      api<Page<Contact>>(`/contacts?limit=8&q=${encodeURIComponent(q.trim())}`).then((p) => setResults(p.items), () => setResults([]));
    }, 250);
    return () => window.clearTimeout(t);
  }, [q, contact]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!contact) return;
    setBusy(true);
    setError(null);
    try {
      onCreated(
        await api<LeadDetail>("/leads", {
          method: "POST",
          body: { contact_id: contact.id, title: title.trim(), value: value.trim() || null, currency: currency.trim() || null },
        }),
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="New lead" description="Leads belong to a contact who has messaged you on WhatsApp.">
      <form onSubmit={submit} className="space-y-4">
        {contact ? (
          <div className="flex items-center justify-between gap-3 rounded-control bg-surface-muted px-3 py-2 text-sm">
            <span>
              <span className="font-semibold">{contactLabel(contact)}</span>
              {contact.phone_e164 && <span className="text-muted"> · {contact.phone_e164}</span>}
            </span>
            <button type="button" className="text-xs font-semibold text-brand hover:underline" onClick={() => setContact(null)}>
              Change
            </button>
          </div>
        ) : (
          <div className="space-y-2">
            <TextField label="Contact" placeholder="Search by name or phone" value={q} onChange={(e) => setQ(e.target.value)} />
            {q.trim().length >= 2 && (
              <ul className="divide-y divide-line rounded-control border border-line">
                {results.length === 0 && <li className="px-3 py-2 text-xs text-muted">No contacts match.</li>}
                {results.map((c) => (
                  <li key={c.id}>
                    <button type="button" className="w-full px-3 py-2 text-left text-sm hover:bg-surface-muted" onClick={() => setContact(c)}>
                      <span className="font-medium">{contactLabel(c)}</span>
                      {c.phone_e164 && <span className="text-muted"> · {c.phone_e164}</span>}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        <div className="grid gap-3 md:grid-cols-[1fr_160px_110px]">
          <TextField label="Title" placeholder="e.g. Teeth whitening package" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} required />
          <TextField label="Value (optional)" inputMode="decimal" pattern="\d+(\.\d{1,2})?" value={value} onChange={(e) => setValue(e.target.value)} />
          <TextField label="Currency" hint="Default: your country's" placeholder="INR" maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
        </div>
        {error && <p className="text-sm text-danger">{error}</p>}
        <div className="flex gap-2">
          <Button type="submit" loading={busy} disabled={!contact}>
            Create lead
          </Button>
          <Button type="button" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}

function LeadPanel({
  leadId,
  members,
  onChanged,
  onDeleted,
}: {
  leadId: string;
  members: Member[];
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const { can, me } = useAuth();
  const [lead, setLead] = useState<LeadDetail | null>(null);
  const [draft, setDraft] = useState({ title: "", status: "new" as LeadStatus, value: "", currency: "", owner: "" });
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const myId = me?.user.id;
  const canAssignOthers = can("conversations:assign");

  const apply = useCallback((l: LeadDetail) => {
    setLead(l);
    setDraft({ title: l.title, status: l.status, value: l.value ?? "", currency: l.currency ?? "", owner: l.owner?.id ?? "" });
  }, []);

  useEffect(() => {
    api<LeadDetail>(`/leads/${leadId}`).then(apply, (err) => setError(errorMessage(err)));
  }, [leadId, apply]);

  async function act(key: string, fn: () => Promise<LeadDetail | void>) {
    setBusy(key);
    setError(null);
    try {
      const result = await fn();
      if (result) apply(result);
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  if (!lead) return error ? <Alert tone="error">{error}</Alert> : <PageLoader />;

  const changes: Record<string, unknown> = {};
  if (draft.title.trim() !== lead.title) changes.title = draft.title.trim();
  if (draft.status !== lead.status) changes.status = draft.status;
  const draftValue = draft.value.trim() || null;
  if ((draftValue === null) !== (lead.value === null) || (draftValue !== null && Number(draftValue) !== Number(lead.value)))
    changes.value = draftValue;
  if ((draft.currency.trim() || null) !== lead.currency) changes.currency = draft.currency.trim() || null;
  if ((draft.owner || null) !== (lead.owner?.id ?? null)) changes.owner_user_id = draft.owner || null;

  // Without the assign permission a person can take an unowned lead or let go of their own.
  const ownerOptions = canAssignOthers
    ? [{ value: "", label: "No owner" }, ...members.map((m) => ({ value: m.user_id, label: m.user_id === myId ? `${m.full_name} (you)` : m.full_name }))]
    : [
        { value: "", label: "No owner" },
        ...(lead.owner && lead.owner.id !== myId ? [{ value: lead.owner.id, label: lead.owner.name }] : []),
        ...(!lead.owner || lead.owner.id === myId ? [{ value: myId ?? "", label: "Me" }] : []),
      ];

  return (
    <Card
      title={lead.title}
      description={`${lead.contact.name ?? lead.contact.phone_e164 ?? "WhatsApp user"} · ${lead.source === "whatsapp" ? "From WhatsApp" : "Added by hand"} · created ${formatDate(lead.created_at)}`}
      actions={<Badge tone={leadStatus(lead.status).tone}>{leadStatus(lead.status).label}</Badge>}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void act("save", () => api<LeadDetail>(`/leads/${lead.id}`, { method: "PATCH", body: changes }));
        }}
        className="space-y-3"
      >
        <TextField label="Title" value={draft.title} maxLength={200} onChange={(e) => setDraft({ ...draft, title: e.target.value })} required />
        <div className="grid grid-cols-2 gap-3">
          <SelectField
            label="Status"
            value={draft.status}
            onChange={(e) => setDraft({ ...draft, status: e.target.value as LeadStatus })}
            options={LEAD_STATUSES.map((s) => ({ value: s.value, label: s.label }))}
          />
          <SelectField
            label="Owner"
            value={draft.owner}
            disabled={!canAssignOthers && !!lead.owner && lead.owner.id !== myId}
            onChange={(e) => setDraft({ ...draft, owner: e.target.value })}
            options={ownerOptions}
          />
        </div>
        <div className="grid grid-cols-[1fr_100px] gap-3">
          <TextField label="Value" inputMode="decimal" pattern="\d+(\.\d{1,2})?" value={draft.value} onChange={(e) => setDraft({ ...draft, value: e.target.value })} />
          <TextField label="Currency" maxLength={3} value={draft.currency} onChange={(e) => setDraft({ ...draft, currency: e.target.value.toUpperCase() })} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" loading={busy === "save"} disabled={Object.keys(changes).length === 0}>
            Save changes
          </Button>
          {lead.conversation_id && (
            <Link href={`/conversations?c=${lead.conversation_id}`} className="text-sm font-semibold text-brand hover:underline">
              Open conversation
            </Link>
          )}
        </div>
      </form>

      <div className="mt-6 border-t border-line pt-5">
        <h3 className="text-sm font-semibold">History</h3>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void act("note", async () => {
              const r = await api<LeadDetail>(`/leads/${lead.id}/notes`, { method: "POST", body: { body: note } });
              setNote("");
              return r;
            });
          }}
          className="mt-3 space-y-2"
        >
          <TextAreaField label="Add a note" rows={2} maxLength={4000} value={note} onChange={(e) => setNote(e.target.value)} />
          <Button type="submit" variant="secondary" className="!px-4 !py-1.5 text-xs" loading={busy === "note"} disabled={!note.trim()}>
            Add note
          </Button>
        </form>
        <ol className="mt-4 space-y-3">
          {lead.activities.map((a) => (
            <li key={a.id} className="border-l-2 border-line pl-3 text-sm">
              <p>{describe(a, members)}</p>
              <p className="text-[11px] text-muted">
                {a.actor?.name ?? "System"} · {formatDateTime(a.created_at)}
              </p>
            </li>
          ))}
        </ol>
      </div>

      {error && <p className="mt-4 text-sm text-danger">{error}</p>}
      {can("conversations:assign") && (
        <div className="mt-6 border-t border-line pt-4">
          <Button
            variant="danger"
            className="!px-4 !py-1.5 text-xs"
            loading={busy === "delete"}
            onClick={() => {
              if (!window.confirm("Delete this lead and its history? Marking it lost keeps the record.")) return;
              void act("delete", async () => {
                await api(`/leads/${lead.id}`, { method: "DELETE" });
                onDeleted();
              });
            }}
          >
            Delete lead
          </Button>
        </div>
      )}
    </Card>
  );
}

function describe(a: LeadActivity, members: Member[]): string {
  const name = (id: string | null) => (id ? (members.find((m) => m.user_id === id)?.full_name ?? "a former member") : "no one");
  const status = (s: string | null) => (s ? leadStatus(s as LeadStatus).label : "—");
  switch (a.type) {
    case "created":
      return `Created (${a.data.source === "whatsapp" ? "from WhatsApp" : "by hand"})`;
    case "status_changed":
      return `Status ${status(a.data.from)} → ${status(a.data.to)}`;
    case "owner_changed":
      return `Owner ${name(a.data.from)} → ${name(a.data.to)}`;
    case "value_changed":
      return `Value ${formatMoney(a.data.from, a.data.previous_currency)} → ${formatMoney(a.data.to, a.data.currency)}`;
    case "title_changed":
      return `Renamed from “${a.data.from}”`;
    case "note":
      return a.data.body ?? "";
    default:
      return a.type;
  }
}
