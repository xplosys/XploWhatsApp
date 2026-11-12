"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type { ChatMessage, Conversation, MessageTemplate, Page, WhatsAppAccount } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatDateTime } from "@/lib/locale";
import { placeholderCount, templateFields } from "@/lib/whatsapp/templates";

// No live socket yet: the list and the open thread refresh on a short interval.
const LIST_REFRESH_MS = 15_000;
const THREAD_REFRESH_MS = 5_000;

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  sending: "Sending",
  accepted: "Sent to WhatsApp",
  sent: "Sent",
  delivered: "Delivered",
  read: "Read",
  failed: "Failed",
};

function contactLabel(c: Conversation["contact"]): string {
  return c.name || c.profile_name || c.phone_e164 || (c.username ? `@${c.username}` : null) || "WhatsApp user";
}

export default function ConversationsPage() {
  const { can } = useAuth();
  const [conversations, setConversations] = useState<Conversation[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadList = useCallback(
    () =>
      api<Page<Conversation>>("/conversations?limit=100").then(
        (page) => {
          setConversations(page.items);
          setError(null);
        },
        (err) => setError(errorMessage(err)),
      ),
    [],
  );

  useEffect(() => {
    const t = window.setInterval(loadList, LIST_REFRESH_MS);
    void Promise.resolve().then(loadList);
    return () => window.clearInterval(t);
  }, [loadList]);

  const selected = conversations?.find((c) => c.id === selectedId) ?? null;

  if (conversations === null && !error) return <PageLoader />;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Conversations</h1>
        <p className="mt-1 text-sm text-muted">Messages with your customers on WhatsApp.</p>
      </header>
      {error && <Alert tone="error">{error}</Alert>}
      {conversations && conversations.length === 0 ? (
        <EmptyState
          title="No conversations yet"
          description="When a customer messages one of your WhatsApp numbers, or you send them a template, the conversation appears here."
        />
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(260px,340px)_1fr]">
          <ul className="max-h-[70vh] divide-y divide-line overflow-y-auto rounded-card border border-line bg-surface shadow-card">
            {(conversations ?? []).map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => setSelectedId(c.id)}
                  className={`w-full px-4 py-3 text-left transition-colors ${
                    c.id === selectedId ? "bg-brand-soft" : "hover:bg-surface-muted"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-semibold">{contactLabel(c.contact)}</span>
                    {c.unread_count > 0 && <Badge tone="brand">{c.unread_count}</Badge>}
                  </div>
                  <p className="mt-0.5 truncate text-xs text-muted">{c.last_message_preview ?? "No messages"}</p>
                  <p className="mt-1 text-[11px] text-muted">
                    {formatDateTime(c.last_message_at)} · {c.service_window_open ? "Reply window open" : "Template needed"}
                  </p>
                </button>
              </li>
            ))}
          </ul>
          {selected ? (
            <Thread
              key={selected.id}
              conversation={selected}
              canReply={can("conversations:reply")}
              canManageContacts={can("contacts:manage")}
              onChanged={loadList}
            />
          ) : (
            <div className="hidden lg:block">
              <EmptyState title="Select a conversation" description="Pick a conversation on the left to read and reply." />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Thread({
  conversation,
  canReply,
  canManageContacts,
  onChanged,
}: {
  conversation: Conversation;
  canReply: boolean;
  canManageContacts: boolean;
  onChanged: () => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const lastCount = useRef(0);
  const unread = conversation.unread_count;

  const load = useCallback(
    () =>
      api<Page<ChatMessage>>(`/conversations/${conversation.id}/messages?limit=100`).then(
        (page) => {
          setMessages(page.items);
          setError(null);
        },
        (err) => setError(errorMessage(err)),
      ),
    [conversation.id],
  );

  useEffect(() => {
    void Promise.resolve().then(load);
    const t = window.setInterval(load, THREAD_REFRESH_MS);
    return () => window.clearInterval(t);
  }, [load]);

  // Opening a conversation with unread messages marks it read.
  useEffect(() => {
    if (unread > 0) api(`/conversations/${conversation.id}/read`, { method: "POST" }).then(onChanged, () => undefined);
  }, [conversation.id, unread, onChanged]);

  useEffect(() => {
    if (messages && messages.length !== lastCount.current) {
      lastCount.current = messages.length;
      bottomRef.current?.scrollIntoView({ block: "end" });
    }
  }, [messages]);

  const contact = conversation.contact;

  return (
    <section className="flex max-h-[70vh] min-h-[420px] flex-col rounded-card border border-line bg-surface shadow-card">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-5 py-4">
        <div>
          <h2 className="text-base font-semibold">{contactLabel(contact)}</h2>
          <p className="text-xs text-muted">
            {contact.phone_e164 ?? "Phone number hidden by the customer"}
            {conversation.number_display ? ` · to ${conversation.number_display}` : ""}
          </p>
        </div>
        <ConsentControl contact={contact} canManage={canManageContacts} onChanged={onChanged} />
      </header>
      <div className="flex-1 space-y-3 overflow-y-auto px-5 py-4">
        {error && <Alert tone="error">{error}</Alert>}
        {messages === null && !error && <PageLoader />}
        {messages?.length === 0 && <p className="text-sm text-muted">No messages in this conversation yet.</p>}
        {messages?.map((m) => <Bubble key={m.id} message={m} />)}
        <div ref={bottomRef} />
      </div>
      {canReply ? (
        <Composer conversation={conversation} onSent={load} />
      ) : (
        <p className="border-t border-line px-5 py-3 text-xs text-muted">Your role can read conversations but not reply.</p>
      )}
    </section>
  );
}

function Bubble({ message: m }: { message: ChatMessage }) {
  const inbound = m.direction === "inbound";
  const text = m.body ?? (m.type === "text" ? "" : `[${m.type}]`);
  return (
    <div className={`flex ${inbound ? "justify-start" : "justify-end"}`}>
      <div
        className={`max-w-[80%] rounded-2xl px-4 py-2 text-sm ${
          inbound ? "bg-surface-muted text-ink" : "bg-brand-soft text-brand-ink"
        }`}
      >
        {m.type !== "text" && m.body && <p className="mb-0.5 text-[11px] uppercase tracking-wide text-muted">{m.type}</p>}
        <p className="whitespace-pre-wrap break-words">{text}</p>
        <p className="mt-1 text-[11px] text-muted">
          {formatDateTime(m.wa_timestamp ?? m.created_at)}
          {!inbound && ` · ${STATUS_LABEL[m.status] ?? m.status}`}
          {m.sender_type === "ai" && " · AI"}
        </p>
        {m.status === "failed" && (
          <p className="mt-1 text-xs text-danger">
            {m.error_message ?? "WhatsApp did not deliver this message."}
            {m.error_code ? ` (code ${m.error_code})` : ""}
          </p>
        )}
      </div>
    </div>
  );
}

function ConsentControl({
  contact,
  canManage,
  onChanged,
}: {
  contact: Conversation["contact"];
  canManage: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const label = { opted_in: "Opted in to marketing", opted_out: "Opted out", unknown: "Marketing consent unknown" }[
    contact.consent_status
  ];
  const tone = { opted_in: "success", opted_out: "danger", unknown: "neutral" } as const;

  async function change(status: "opted_in" | "opted_out") {
    const verb = status === "opted_in" ? "record that this customer opted in to marketing messages" : "opt this customer out of all messages";
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
    <div className="flex flex-col items-end gap-1.5">
      <Badge tone={tone[contact.consent_status]}>{label}</Badge>
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

function Composer({ conversation, onSent }: { conversation: Conversation; onSent: () => void }) {
  const windowOpen = conversation.service_window_open;
  const optedOut = conversation.contact.consent_status === "opted_out";
  const [templates, setTemplates] = useState<MessageTemplate[] | null>(null);
  const [chosenMode, setMode] = useState<"text" | "template">("text");
  // Outside the 24-hour window only templates are allowed, whatever was chosen before.
  const mode = windowOpen ? chosenMode : "template";
  const [text, setText] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [params, setParams] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Templates belong to the WhatsApp account that owns this conversation's number.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const accounts = await api<WhatsAppAccount[]>("/whatsapp/accounts");
        const account = accounts.find((a) => a.numbers.some((n) => n.id === conversation.number_id));
        const list = account
          ? await api<MessageTemplate[]>(`/whatsapp/templates?account_id=${account.id}&approved_only=true`)
          : [];
        if (!cancelled) {
          setTemplates(list);
          setTemplateId((cur) => cur || list[0]?.id || "");
        }
      } catch {
        if (!cancelled) setTemplates([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversation.number_id]);

  const template = useMemo(() => templates?.find((t) => t.id === templateId), [templates, templateId]);
  const count = placeholderCount(template);

  async function send(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body = mode === "template" && template ? templateFields(template, params) : { text };
      await api(`/conversations/${conversation.id}/messages`, { method: "POST", body });
      setText("");
      setParams([]);
      onSent();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (optedOut) {
    return (
      <p className="border-t border-line px-5 py-3 text-xs text-muted">
        This customer opted out. Nothing can be sent until they message START or you record new consent.
      </p>
    );
  }

  const noTemplates = mode === "template" && templates !== null && templates.length === 0;

  return (
    <form onSubmit={send} className="space-y-3 border-t border-line px-5 py-4">
      {!windowOpen && (
        <p className="text-xs text-muted">
          More than 24 hours have passed since this customer last wrote, so WhatsApp only allows an approved template.
        </p>
      )}
      <div className="grid gap-3 md:grid-cols-[180px_1fr]">
        <SelectField
          label="Send as"
          value={mode}
          onChange={(e) => setMode(e.target.value as "text" | "template")}
          options={[
            ...(windowOpen ? [{ value: "text", label: "Text reply" }] : []),
            { value: "template", label: "Approved template" },
          ]}
        />
        {mode === "text" ? (
          <TextField label="Message" value={text} onChange={(e) => setText(e.target.value)} maxLength={4096} required />
        ) : templates === null ? (
          <p className="self-end pb-3 text-sm text-muted">Loading templates…</p>
        ) : noTemplates ? (
          <p className="self-end pb-3 text-sm text-muted">
            No approved templates for this number. Create one in WhatsApp Manager, then sync templates on the WhatsApp page.
          </p>
        ) : (
          <SelectField
            label="Template"
            value={templateId}
            onChange={(e) => {
              setTemplateId(e.target.value);
              setParams([]);
            }}
            options={templates.map((t) => ({ value: t.id, label: `${t.name} (${t.language})` }))}
          />
        )}
      </div>
      {mode === "template" && template && (
        <>
          <p className="rounded-control bg-surface-muted px-3 py-2 text-xs text-muted">
            {template.components.find((c) => c.type === "BODY")?.text ?? template.name}
          </p>
          {count > 0 && (
            <div className="grid gap-3 md:grid-cols-2">
              {Array.from({ length: count }, (_, i) => (
                <TextField
                  key={i}
                  label={`Value for {{${i + 1}}}`}
                  value={params[i] ?? ""}
                  onChange={(e) => setParams((p) => Object.assign([...p], { [i]: e.target.value }))}
                  required
                />
              ))}
            </div>
          )}
        </>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" loading={busy} disabled={noTemplates || (mode === "template" && !template)}>
          Send
        </Button>
        {error && <span className="text-sm text-danger">{error}</span>}
      </div>
    </form>
  );
}
