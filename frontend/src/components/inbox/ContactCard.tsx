"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/Field";
import { api, errorMessage } from "@/lib/api/client";
import type { ContactDetail, LeadDetail } from "@/lib/api/types";
import { contactLabel, formatMoney, leadStatus } from "@/lib/inbox";
import { formatDateTime } from "@/lib/locale";

const CONSENT = {
  opted_in: { label: "Opted in to marketing", tone: "success" },
  opted_out: { label: "Opted out", tone: "danger" },
  unknown: { label: "Marketing consent unknown", tone: "neutral" },
} as const;

/** A contact's details, tags, consent and leads. Editable with contacts:manage;
 * leads need leads:manage. `conversationId` links a new lead to that conversation. */
export function ContactCard({
  contactId,
  conversationId,
  canManage,
  canLeads,
  onChanged,
}: {
  contactId: string;
  conversationId?: string;
  canManage: boolean;
  canLeads: boolean;
  onChanged?: () => void;
}) {
  const [contact, setContact] = useState<ContactDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);

  const load = useCallback(
    () =>
      api<ContactDetail>(`/contacts/${contactId}`).then(
        (c) => {
          setContact(c);
          setError(null);
        },
        (err) => setError(errorMessage(err)),
      ),
    [contactId],
  );

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  if (!contact) {
    return <PanelSection title="Contact">{error ? <p className="text-xs text-danger">{error}</p> : <p className="text-xs text-muted">Loading…</p>}</PanelSection>;
  }

  const changed = () => {
    void load();
    onChanged?.();
  };

  return (
    <>
      <PanelSection
        title="Contact"
        action={
          canManage && !editing ? (
            <button type="button" className="text-xs font-semibold text-brand hover:underline" onClick={() => setEditing(true)}>
              Edit
            </button>
          ) : null
        }
      >
        {editing ? (
          <ContactEditor
            contact={contact}
            onDone={(updated) => {
              setEditing(false);
              if (updated) {
                setContact(updated);
                onChanged?.();
              }
            }}
          />
        ) : (
          <dl className="space-y-1.5 text-sm">
            <Row label="Name">{contactLabel(contact)}</Row>
            {contact.name && contact.profile_name && <Row label="WhatsApp name">{contact.profile_name}</Row>}
            <Row label="Phone">{contact.phone_e164 ?? "Hidden by the customer"}</Row>
            {contact.username && <Row label="Username">@{contact.username}</Row>}
            <Row label="Language">{contact.language ?? "Not set"}</Row>
            <Row label="First seen">{formatDateTime(contact.created_at)}</Row>
            <div className="flex flex-wrap gap-1.5 pt-1">
              {contact.tags.length ? (
                contact.tags.map((t) => (
                  <Badge key={t} tone="brand">
                    {t}
                  </Badge>
                ))
              ) : (
                <span className="text-xs text-muted">No tags</span>
              )}
            </div>
          </dl>
        )}
        <Consent contact={contact} canManage={canManage} onChanged={changed} />
        {contact.conversations.length > 1 && (
          <p className="mt-3 text-xs text-muted">
            Also writes to {contact.conversations.length - 1} other number{contact.conversations.length > 2 ? "s" : ""} of
            yours.
          </p>
        )}
      </PanelSection>
      {canLeads && <ContactLeads contact={contact} conversationId={conversationId} onChanged={changed} />}
    </>
  );
}

export function PanelSection({
  title,
  action,
  children,
}: {
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-card border border-line bg-surface p-4 shadow-card">
      <header className="mb-3 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        {action}
      </header>
      {children}
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-muted">{label}</dt>
      <dd className="truncate text-right font-medium">{children}</dd>
    </div>
  );
}

function ContactEditor({ contact, onDone }: { contact: ContactDetail; onDone: (c: ContactDetail | null) => void }) {
  const [name, setName] = useState(contact.name ?? "");
  const [language, setLanguage] = useState(contact.language ?? "");
  const [tags, setTags] = useState<string[]>(contact.tags);
  const [tagInput, setTagInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function addTag() {
    const t = tagInput.trim().toLowerCase().replace(/\s+/g, " ");
    if (t && !tags.includes(t)) setTags([...tags, t]);
    setTagInput("");
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const pending = tagInput.trim() ? [...tags, tagInput.trim().toLowerCase()] : tags;
    try {
      const updated = await api<ContactDetail>(`/contacts/${contact.id}`, {
        method: "PATCH",
        body: { name: name.trim() || null, language: language.trim() || null, tags: pending },
      });
      onDone(updated);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={save} className="space-y-3">
      <TextField
        label="Name"
        hint={contact.profile_name ? `WhatsApp name: ${contact.profile_name}` : undefined}
        value={name}
        maxLength={200}
        onChange={(e) => setName(e.target.value)}
      />
      <TextField
        label="Language"
        hint="A code such as en, hi or en-GB"
        value={language}
        maxLength={16}
        onChange={(e) => setLanguage(e.target.value)}
      />
      <div className="space-y-1.5">
        <TextField
          label="Tags"
          hint="Press Enter to add. Letters, numbers, spaces and - _ . & + /"
          value={tagInput}
          maxLength={40}
          onChange={(e) => setTagInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              addTag();
            }
          }}
        />
        <div className="flex flex-wrap gap-1.5">
          {tags.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setTags(tags.filter((x) => x !== t))}
              className="inline-flex items-center gap-1 rounded-full bg-brand-soft px-2.5 py-0.5 text-xs font-medium text-brand-ink hover:bg-danger-soft hover:text-danger"
              aria-label={`Remove tag ${t}`}
            >
              {t} <span aria-hidden>×</span>
            </button>
          ))}
        </div>
      </div>
      {error && <p className="text-xs text-danger">{error}</p>}
      <div className="flex gap-2">
        <Button type="submit" className="!px-4 !py-1.5 text-xs" loading={busy}>
          Save
        </Button>
        <Button type="button" variant="ghost" className="!px-4 !py-1.5 text-xs" onClick={() => onDone(null)}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function Consent({ contact, canManage, onChanged }: { contact: ContactDetail; canManage: boolean; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const c = CONSENT[contact.consent_status];

  async function change(status: "opted_in" | "opted_out") {
    const verb =
      status === "opted_in"
        ? "record that this customer opted in to marketing messages"
        : "opt this customer out of all messages";
    if (!window.confirm(`Do you want to ${verb}?`)) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/contacts/${contact.id}/consent`, { method: "PUT", body: { status, source: "agent dashboard" } });
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 space-y-1.5 border-t border-line pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={c.tone}>{c.label}</Badge>
        {contact.consent_source && <span className="text-[11px] text-muted">via {contact.consent_source}</span>}
      </div>
      {canManage && (
        <div className="flex gap-2">
          {contact.consent_status !== "opted_in" && (
            <Button variant="ghost" className="!px-3 !py-1 text-xs" loading={busy} onClick={() => change("opted_in")}>
              Mark opted in
            </Button>
          )}
          {contact.consent_status !== "opted_out" && (
            <Button variant="ghost" className="!px-3 !py-1 text-xs" loading={busy} onClick={() => change("opted_out")}>
              Opt out
            </Button>
          )}
        </div>
      )}
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}

function ContactLeads({
  contact,
  conversationId,
  onChanged,
}: {
  contact: ContactDetail;
  conversationId?: string;
  onChanged: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api<LeadDetail>("/leads", {
        method: "POST",
        body: {
          ...(conversationId ? { conversation_id: conversationId } : { contact_id: contact.id }),
          title: title.trim(),
          value: value.trim() || null,
        },
      });
      setTitle("");
      setValue("");
      setAdding(false);
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <PanelSection
      title="Leads"
      action={
        !adding ? (
          <button type="button" className="text-xs font-semibold text-brand hover:underline" onClick={() => setAdding(true)}>
            New lead
          </button>
        ) : null
      }
    >
      {adding && (
        <form onSubmit={create} className="mb-3 space-y-2">
          <TextField label="What are they interested in?" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} required />
          <TextField
            label="Estimated value (optional)"
            hint="In your business's currency; change it on the Leads page"
            inputMode="decimal"
            pattern="\d+(\.\d{1,2})?"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          {error && <p className="text-xs text-danger">{error}</p>}
          <div className="flex gap-2">
            <Button type="submit" className="!px-4 !py-1.5 text-xs" loading={busy}>
              Create lead
            </Button>
            <Button type="button" variant="ghost" className="!px-4 !py-1.5 text-xs" onClick={() => setAdding(false)}>
              Cancel
            </Button>
          </div>
        </form>
      )}
      {contact.leads.length === 0 && !adding ? (
        <p className="text-xs text-muted">No leads for this contact yet.</p>
      ) : (
        <ul className="space-y-2">
          {contact.leads.map((l) => (
            <li key={l.id}>
              <Link href={`/leads?lead=${l.id}`} className="block rounded-control border border-line px-3 py-2 hover:border-ring">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium">{l.title}</span>
                  <Badge tone={leadStatus(l.status).tone}>{leadStatus(l.status).label}</Badge>
                </div>
                <p className="mt-0.5 text-xs text-muted">
                  {formatMoney(l.value, l.currency)} · {l.owner ? l.owner.name : "No owner"}
                </p>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </PanelSection>
  );
}
