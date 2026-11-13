"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ContactCard, PanelSection } from "@/components/inbox/ContactCard";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type { Contact, ContactDetail, Page, TagCount } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { contactLabel } from "@/lib/inbox";
import { formatDate, formatDateTime } from "@/lib/locale";

const PAGE_SIZE = 50;

interface Filters {
  q: string;
  tag: string;
  consent: string;
  offset: number;
}

export default function ContactsPage() {
  const { can } = useAuth();
  const [filters, setFilters] = useState<Filters>({ q: "", tag: "", consent: "", offset: 0 });
  const [search, setSearch] = useState("");
  const [page, setPage] = useState<Page<Contact> | null>(null);
  const [tags, setTags] = useState<TagCount[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    const p = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(filters.offset) });
    if (filters.q.trim().length >= 2) p.set("q", filters.q.trim());
    if (filters.tag) p.set("tag", filters.tag);
    if (filters.consent) p.set("consent", filters.consent);
    return Promise.all([api<Page<Contact>>(`/contacts?${p}`), api<TagCount[]>("/contacts/tags")]).then(
      ([result, t]) => {
        setPage(result);
        setTags(t);
        setError(null);
      },
      (err) => setError(errorMessage(err)),
    );
  }, [filters]);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  useEffect(() => {
    const t = window.setTimeout(() => setFilters((f) => (f.q === search ? f : { ...f, q: search, offset: 0 })), 300);
    return () => window.clearTimeout(t);
  }, [search]);

  if (!page && !error) return <PageLoader />;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Contacts</h1>
        <p className="mt-1 text-sm text-muted">Everyone who has written to your WhatsApp numbers, with your notes on them.</p>
      </header>
      {error && <Alert tone="error">{error}</Alert>}
      <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
        <Card>
          <div className="mb-4 grid gap-3 sm:grid-cols-[1fr_180px_200px]">
            <TextField label="Search" placeholder="Name, phone or username" value={search} maxLength={100} onChange={(e) => setSearch(e.target.value)} />
            <SelectField
              label="Tag"
              value={filters.tag}
              onChange={(e) => setFilters((f) => ({ ...f, tag: e.target.value, offset: 0 }))}
              options={[{ value: "", label: "All tags" }, ...tags.map((t) => ({ value: t.tag, label: `${t.tag} (${t.contacts})` }))]}
            />
            <SelectField
              label="Marketing consent"
              value={filters.consent}
              onChange={(e) => setFilters((f) => ({ ...f, consent: e.target.value, offset: 0 }))}
              options={[
                { value: "", label: "Any" },
                { value: "opted_in", label: "Opted in" },
                { value: "unknown", label: "Unknown" },
                { value: "opted_out", label: "Opted out" },
              ]}
            />
          </div>
          {page && page.items.length === 0 ? (
            <EmptyState
              title={filters.q || filters.tag || filters.consent ? "No contacts match" : "No contacts yet"}
              description="Contacts are added when someone messages one of your WhatsApp numbers."
            />
          ) : (
            <div className="-mx-6 overflow-x-auto">
              <table className="w-full min-w-[600px] text-sm">
                <thead className="text-left text-xs text-muted">
                  <tr className="border-b border-line">
                    <th className="px-6 py-2 font-medium">Contact</th>
                    <th className="px-3 py-2 font-medium">Tags</th>
                    <th className="px-3 py-2 font-medium">Consent</th>
                    <th className="px-6 py-2 font-medium">Last message</th>
                  </tr>
                </thead>
                <tbody>
                  {page?.items.map((c) => (
                    <tr
                      key={c.id}
                      onClick={() => setSelectedId(c.id)}
                      className={`cursor-pointer border-b border-line last:border-0 ${c.id === selectedId ? "bg-brand-soft" : "hover:bg-surface-muted"}`}
                    >
                      <td className="px-6 py-3">
                        <button type="button" className="text-left font-semibold hover:text-brand" onClick={() => setSelectedId(c.id)}>
                          {contactLabel(c)}
                        </button>
                        <p className="text-xs text-muted">{c.phone_e164 ?? (c.username ? `@${c.username}` : "Phone hidden")}</p>
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex flex-wrap gap-1">
                          {c.tags.slice(0, 4).map((t) => (
                            <Badge key={t} tone="brand">
                              {t}
                            </Badge>
                          ))}
                          {c.tags.length > 4 && <span className="text-xs text-muted">+{c.tags.length - 4}</span>}
                        </div>
                      </td>
                      <td className="px-3 py-3 text-xs">
                        {{ opted_in: "Opted in", opted_out: "Opted out", unknown: "Unknown" }[c.consent_status]}
                      </td>
                      <td className="px-6 py-3 text-xs text-muted">{c.last_inbound_at ? formatDate(c.last_inbound_at) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {page && page.total > PAGE_SIZE && (
            <div className="mt-4 flex items-center justify-between text-xs text-muted">
              <span>
                {page.offset + 1}–{page.offset + page.items.length} of {page.total}
              </span>
              <div className="flex gap-3">
                <button
                  type="button"
                  className="font-semibold text-brand disabled:text-muted"
                  disabled={page.offset === 0}
                  onClick={() => setFilters((f) => ({ ...f, offset: Math.max(0, f.offset - PAGE_SIZE) }))}
                >
                  Previous
                </button>
                <button
                  type="button"
                  className="font-semibold text-brand disabled:text-muted"
                  disabled={page.offset + PAGE_SIZE >= page.total}
                  onClick={() => setFilters((f) => ({ ...f, offset: f.offset + PAGE_SIZE }))}
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </Card>
        {selectedId ? (
          <div key={selectedId} className="space-y-4">
            <ContactCard contactId={selectedId} canManage={can("contacts:manage")} canLeads={can("leads:manage")} onChanged={load} />
            <ContactConversations contactId={selectedId} />
          </div>
        ) : (
          <div className="hidden xl:block">
            <EmptyState title="Select a contact" description="Pick a contact to see their details, tags, conversations and leads." />
          </div>
        )}
      </div>
    </div>
  );
}

function ContactConversations({ contactId }: { contactId: string }) {
  const [detail, setDetail] = useState<ContactDetail | null>(null);

  useEffect(() => {
    api<ContactDetail>(`/contacts/${contactId}`).then(setDetail, () => setDetail(null));
  }, [contactId]);

  if (!detail?.conversations.length) return null;
  return (
    <PanelSection title="Conversations">
      <ul className="space-y-2">
        {detail.conversations.map((c) => (
          <li key={c.id}>
            <Link href={`/conversations?c=${c.id}`} className="block rounded-control border border-line px-3 py-2 text-sm hover:border-ring">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium">To {c.number_display ?? "your number"}</span>
                {c.status === "closed" ? <Badge>Closed</Badge> : c.needs_human ? <Badge tone="warning">Needs a person</Badge> : null}
              </div>
              <p className="mt-0.5 text-xs text-muted">
                {formatDateTime(c.last_message_at)} · {c.assigned_to ? c.assigned_to.name : "Unassigned"}
              </p>
            </Link>
          </li>
        ))}
      </ul>
    </PanelSection>
  );
}
