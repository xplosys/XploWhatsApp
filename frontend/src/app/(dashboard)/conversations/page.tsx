"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ContactCard, PanelSection } from "@/components/inbox/ContactCard";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type {
  ChatMessage,
  Conversation,
  ConversationNote,
  ConversationSummary,
  HandoverHistory,
  InboxCounts,
  InboxView,
  Member,
  MessageTemplate,
  Page,
  TagCount,
  WhatsAppAccount,
} from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { contactLabel, handoverReason, INBOX_VIEWS } from "@/lib/inbox";
import { formatDateTime } from "@/lib/locale";
import { placeholderCount, templateFields } from "@/lib/whatsapp/templates";

// No live socket yet: the list and the open thread refresh on a short interval.
const LIST_REFRESH_MS = 15_000;
const THREAD_REFRESH_MS = 5_000;
const SUMMARY_POLL_MS = 3_000;

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  sending: "Sending",
  accepted: "Sent to WhatsApp",
  sent: "Sent",
  delivered: "Delivered",
  read: "Read",
  failed: "Failed",
};

interface Filters {
  view: InboxView;
  q: string;
  tag: string;
}

function listUrl(f: Filters): string {
  const p = new URLSearchParams({ limit: "100", view: f.view });
  if (f.q.trim().length >= 2) p.set("q", f.q.trim());
  if (f.tag) p.set("tag", f.tag);
  return `/conversations?${p}`;
}

export default function ConversationsPage() {
  const { can, me } = useAuth();
  const [filters, setFilters] = useState<Filters>({ view: "open", q: "", tag: "" });
  const [search, setSearch] = useState("");
  const [conversations, setConversations] = useState<Conversation[] | null>(null);
  const [counts, setCounts] = useState<InboxCounts | null>(null);
  const [tags, setTags] = useState<TagCount[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadList = useCallback(
    () =>
      Promise.all([api<Page<Conversation>>(listUrl(filters)), api<InboxCounts>("/conversations/counts")]).then(
        ([page, c]) => {
          setConversations(page.items);
          setCounts(c);
          setError(null);
        },
        (err) => setError(errorMessage(err)),
      ),
    [filters],
  );

  useEffect(() => {
    const t = window.setInterval(loadList, LIST_REFRESH_MS);
    void Promise.resolve().then(loadList);
    return () => window.clearInterval(t);
  }, [loadList]);

  // Debounce typing into the search box.
  useEffect(() => {
    const t = window.setTimeout(() => setFilters((f) => (f.q === search ? f : { ...f, q: search })), 300);
    return () => window.clearTimeout(t);
  }, [search]);

  useEffect(() => {
    api<TagCount[]>("/contacts/tags").then(setTags, () => setTags([]));
    if (can("members:read")) {
      api<Page<Member>>("/tenants/current/members?limit=100").then(
        (p) => setMembers(p.items.filter((m) => m.status === "active")),
        () => setMembers([]),
      );
    }
    // A link such as /conversations?c=<id> opens that conversation.
    const wanted = new URLSearchParams(window.location.search).get("c");
    if (wanted) void Promise.resolve().then(() => setSelectedId(wanted));
  }, [can]);

  if (conversations === null && !error) return <PageLoader />;
  const everEmpty = counts !== null && counts.open + counts.closed === 0;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Conversations</h1>
          <p className="mt-1 text-sm text-muted">Messages with your customers on WhatsApp, and who is answering them.</p>
        </div>
      </header>
      {error && <Alert tone="error">{error}</Alert>}
      {everEmpty ? (
        <EmptyState
          title="No conversations yet"
          description="When a customer messages one of your WhatsApp numbers, or you send them a template, the conversation appears here."
        />
      ) : (
        <div className="grid grid-cols-[minmax(0,1fr)] gap-4 lg:grid-cols-[minmax(260px,320px)_minmax(0,1fr)]">
          {/* On phones the list and the open conversation take turns. */}
          <div className={`min-w-0 space-y-3 ${selectedId ? "hidden lg:block" : ""}`}>
            <nav aria-label="Inbox views" className="flex flex-wrap gap-1.5">
              {INBOX_VIEWS.map((v) => (
                <button
                  key={v.value}
                  type="button"
                  aria-pressed={filters.view === v.value}
                  onClick={() => setFilters((f) => ({ ...f, view: v.value }))}
                  className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold transition-colors ${
                    filters.view === v.value ? "bg-brand-soft text-brand-ink" : "text-muted hover:bg-surface-muted hover:text-ink"
                  }`}
                >
                  {v.label}
                  {counts && <span className="tabular-nums opacity-70">{counts[v.value]}</span>}
                </button>
              ))}
            </nav>
            <div className="grid grid-cols-[1fr_auto] gap-2">
              <input
                type="search"
                aria-label="Search conversations"
                placeholder="Search name, phone or message"
                value={search}
                maxLength={100}
                onChange={(e) => setSearch(e.target.value)}
                className="w-full rounded-control border-[1.5px] border-line bg-surface px-3 py-2 text-sm placeholder:text-muted/70 focus:border-ring focus:outline-none dark:bg-surface-muted"
              />
              <select
                aria-label="Filter by tag"
                value={filters.tag}
                onChange={(e) => setFilters((f) => ({ ...f, tag: e.target.value }))}
                className="rounded-control border-[1.5px] border-line bg-surface px-2 py-2 text-sm focus:border-ring focus:outline-none dark:bg-surface-muted"
              >
                <option value="">All tags</option>
                {tags.map((t) => (
                  <option key={t.tag} value={t.tag}>
                    {t.tag} ({t.contacts})
                  </option>
                ))}
              </select>
            </div>
            <ul className="max-h-[70vh] divide-y divide-line overflow-y-auto rounded-card border border-line bg-surface shadow-card">
              {conversations?.length === 0 && (
                <li className="px-4 py-8 text-center text-sm text-muted">
                  {filters.q.trim().length >= 2 || filters.tag ? "Nothing matches this search." : "Nothing here right now."}
                </li>
              )}
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
                      <span className="flex shrink-0 gap-1">
                        {c.needs_human && <Badge tone="warning">Needs a person</Badge>}
                        {c.unread_count > 0 && <Badge tone="brand">{c.unread_count}</Badge>}
                      </span>
                    </div>
                    <p className="mt-0.5 truncate text-xs text-muted">{c.last_message_preview ?? "No messages"}</p>
                    <p className="mt-1 truncate text-[11px] text-muted">
                      {formatDateTime(c.last_message_at)}
                      {" · "}
                      {c.assigned_to ? (c.assigned_to.id === me?.user.id ? "You" : c.assigned_to.name) : "Unassigned"}
                      {c.status === "closed" ? " · Closed" : c.awaiting_reply ? " · Waiting for reply" : ""}
                    </p>
                  </button>
                </li>
              ))}
            </ul>
          </div>
          {selectedId ? (
            <Thread
              key={selectedId}
              conversationId={selectedId}
              members={members}
              onChanged={loadList}
              onBack={() => setSelectedId(null)}
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
  conversationId,
  members,
  onChanged,
  onBack,
}: {
  conversationId: string;
  members: Member[];
  onChanged: () => void;
  onBack: () => void;
}) {
  const { can } = useAuth();
  const canReply = can("conversations:reply");
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<ChatMessage[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const lastCount = useRef(0);

  const load = useCallback(
    () =>
      Promise.all([
        api<Conversation>(`/conversations/${conversationId}`),
        api<Page<ChatMessage>>(`/conversations/${conversationId}/messages?limit=100`),
      ]).then(
        ([conv, page]) => {
          setConversation(conv);
          setMessages(page.items);
          setError(null);
        },
        (err) => setError(errorMessage(err)),
      ),
    [conversationId],
  );

  useEffect(() => {
    void Promise.resolve().then(load);
    const t = window.setInterval(load, THREAD_REFRESH_MS);
    return () => window.clearInterval(t);
  }, [load]);

  const unread = conversation?.unread_count ?? 0;
  // Opening a conversation with unread messages marks it read.
  useEffect(() => {
    if (unread > 0) api(`/conversations/${conversationId}/read`, { method: "POST" }).then(onChanged, () => undefined);
  }, [conversationId, unread, onChanged]);

  useEffect(() => {
    if (messages && messages.length !== lastCount.current) {
      lastCount.current = messages.length;
      bottomRef.current?.scrollIntoView({ block: "end" });
    }
  }, [messages]);

  const refresh = useCallback(() => {
    void load();
    onChanged();
  }, [load, onChanged]);

  if (!conversation) {
    return error ? <Alert tone="error">{error}</Alert> : <PageLoader />;
  }
  const contact = conversation.contact;

  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4 2xl:grid-cols-[minmax(0,1fr)_320px]">
      <button type="button" onClick={onBack} className="justify-self-start text-sm font-semibold text-brand hover:underline lg:hidden">
        ← All conversations
      </button>
      <section className="flex max-h-[78vh] min-h-[460px] flex-col rounded-card border border-line bg-surface shadow-card">
        <header className="space-y-3 border-b border-line px-5 py-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="truncate text-base font-semibold">{contactLabel(contact)}</h2>
              <p className="text-xs text-muted">
                {contact.phone_e164 ?? "Phone number hidden by the customer"}
                {conversation.number_display ? ` · to ${conversation.number_display}` : ""}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              {conversation.status === "closed" && <Badge>Closed</Badge>}
              {conversation.needs_human && <Badge tone="warning">Needs a person</Badge>}
              <Badge tone={conversation.ai_enabled ? "success" : "neutral"}>
                {conversation.ai_enabled ? "AI replies on" : "AI replies off"}
              </Badge>
            </div>
          </div>
          {conversation.handover && (
            <p className="rounded-control bg-surface-muted px-3 py-2 text-xs text-muted">
              {handoverReason(conversation.handover.reason)} · {formatDateTime(conversation.handover.started_at)}
              {conversation.handover.agent ? ` · handled by ${conversation.handover.agent.name}` : ""}
            </p>
          )}
          {canReply && <Actions conversation={conversation} members={members} onChanged={refresh} />}
        </header>
        <div className="flex-1 space-y-3 overflow-y-auto px-5 py-4">
          {error && <Alert tone="error">{error}</Alert>}
          {messages?.length === 0 && <p className="text-sm text-muted">No messages in this conversation yet.</p>}
          {messages?.map((m) => <Bubble key={m.id} message={m} />)}
          <div ref={bottomRef} />
        </div>
        {canReply ? (
          <Composer conversation={conversation} onSent={refresh} />
        ) : (
          <p className="border-t border-line px-5 py-3 text-xs text-muted">Your role can read conversations but not reply.</p>
        )}
      </section>
      <aside className="space-y-4">
        <SummaryCard conversationId={conversation.id} canRequest={canReply} />
        <NotesCard conversationId={conversation.id} canWrite={canReply} />
        <ContactCard
          contactId={contact.id}
          conversationId={conversation.id}
          canManage={can("contacts:manage")}
          canLeads={can("leads:manage")}
          onChanged={refresh}
        />
        <HistoryCard conversationId={conversation.id} version={conversation.handover?.started_at ?? String(conversation.ai_enabled)} />
      </aside>
    </div>
  );
}

function Actions({
  conversation,
  members,
  onChanged,
}: {
  conversation: Conversation;
  members: Member[];
  onChanged: () => void;
}) {
  const { can, me } = useAuth();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const myId = me?.user.id;
  const canAssignOthers = can("conversations:assign");
  const assignee = conversation.assigned_to;

  async function run(key: string, path: string, method: "POST" | "PUT", body?: unknown) {
    setBusy(key);
    setError(null);
    try {
      await api(`/conversations/${conversation.id}/${path}`, { method, body });
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  // Agents may only pick up an unassigned conversation or let go of their own.
  const options = canAssignOthers
    ? [{ value: "", label: "Unassigned" }, ...members.map((m) => ({ value: m.user_id, label: m.user_id === myId ? `${m.full_name} (you)` : m.full_name }))]
    : null;

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        {options ? (
          <label className="flex items-center gap-2 text-xs text-muted">
            Assigned to
            <select
              value={assignee?.id ?? ""}
              disabled={busy !== null}
              onChange={(e) => run("assign", "assignee", "PUT", { user_id: e.target.value || null })}
              className="rounded-control border-[1.5px] border-line bg-surface px-2 py-1 text-xs text-ink focus:border-ring focus:outline-none dark:bg-surface-muted"
            >
              {options.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
              {assignee && !options.some((o) => o.value === assignee.id) && <option value={assignee.id}>{assignee.name}</option>}
            </select>
          </label>
        ) : (
          <span className="text-xs text-muted">
            {assignee ? `Assigned to ${assignee.id === myId ? "you" : assignee.name}` : "Unassigned"}
          </span>
        )}
        {!options && !assignee && (
          <Button variant="secondary" className="!px-3 !py-1 text-xs" loading={busy === "assign"} onClick={() => run("assign", "assignee", "PUT", { user_id: myId })}>
            Assign to me
          </Button>
        )}
        {!options && assignee?.id === myId && (
          <Button variant="ghost" className="!px-3 !py-1 text-xs" loading={busy === "assign"} onClick={() => run("assign", "assignee", "PUT", { user_id: null })}>
            Unassign me
          </Button>
        )}
        <span className="mx-1 hidden h-4 w-px bg-line sm:inline-block" aria-hidden />
        {conversation.ai_enabled ? (
          <Button variant="secondary" className="!px-3 !py-1 text-xs" loading={busy === "takeover"} onClick={() => run("takeover", "takeover", "POST")}>
            Take over from AI
          </Button>
        ) : (
          <>
            {conversation.needs_human && (
              <Button variant="secondary" className="!px-3 !py-1 text-xs" loading={busy === "takeover"} onClick={() => run("takeover", "takeover", "POST")}>
                I&apos;m handling this
              </Button>
            )}
            <Button variant="ghost" className="!px-3 !py-1 text-xs" loading={busy === "release"} onClick={() => run("release", "release", "POST")}>
              Hand back to AI
            </Button>
          </>
        )}
        {conversation.status === "open" ? (
          <Button variant="ghost" className="!px-3 !py-1 text-xs" loading={busy === "status"} onClick={() => run("status", "status", "PUT", { status: "closed" })}>
            Close
          </Button>
        ) : (
          <Button variant="ghost" className="!px-3 !py-1 text-xs" loading={busy === "status"} onClick={() => run("status", "status", "PUT", { status: "open" })}>
            Reopen
          </Button>
        )}
      </div>
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}

function SummaryCard({ conversationId, canRequest }: { conversationId: string; canRequest: boolean }) {
  const [summary, setSummary] = useState<ConversationSummary | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api<ConversationSummary | null>(`/conversations/${conversationId}/summary`).then(
        (s) => setSummary(s),
        (err) => setError(errorMessage(err)),
      ),
    [conversationId],
  );

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  // While a summary is being written, check again every few seconds.
  const queued = summary?.status === "queued";
  useEffect(() => {
    if (!queued) return;
    const t = window.setInterval(load, SUMMARY_POLL_MS);
    return () => window.clearInterval(t);
  }, [queued, load]);

  async function request() {
    setBusy(true);
    setError(null);
    try {
      setSummary(await api<ConversationSummary>(`/conversations/${conversationId}/summary`, { method: "POST" }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const label = !summary ? "Summarise" : summary.status === "ready" ? "Refresh" : "Try again";

  return (
    <PanelSection
      title="AI summary"
      action={
        canRequest && !queued ? (
          <Button variant="ghost" className="!px-3 !py-1 text-xs" loading={busy} onClick={request}>
            {label}
          </Button>
        ) : null
      }
    >
      {summary === undefined && <p className="text-xs text-muted">Loading…</p>}
      {summary === null && (
        <p className="text-xs text-muted">
          No summary yet. One is written automatically when the assistant hands over, or ask for one now.
        </p>
      )}
      {queued && <p className="text-xs text-muted">Writing a summary…</p>}
      {summary?.status === "failed" && <p className="text-xs text-danger">{summary.error ?? "The summary could not be written."}</p>}
      {summary?.status === "ready" && summary.summary && (
        <>
          <p className="whitespace-pre-wrap text-sm">{summary.summary}</p>
          <p className="mt-2 text-[11px] text-muted">
            {formatDateTime(summary.generated_at)} · {summary.model}
            {summary.new_messages > 0 &&
              ` · ${summary.new_messages} new message${summary.new_messages > 1 ? "s" : ""} since`}
          </p>
        </>
      )}
      {error && <p className="mt-2 text-xs text-danger">{error}</p>}
    </PanelSection>
  );
}

function NotesCard({ conversationId, canWrite }: { conversationId: string; canWrite: boolean }) {
  const [notes, setNotes] = useState<ConversationNote[] | null>(null);
  const [text, setText] = useState("");
  const [editing, setEditing] = useState<{ id: string; body: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const base = `/conversations/${conversationId}/notes`;

  const load = useCallback(
    () => api<ConversationNote[]>(base).then(setNotes, (err) => setError(errorMessage(err))),
    [base],
  );

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <PanelSection title="Internal notes">
      <p className="mb-3 text-[11px] text-muted">Only your team sees these. They are never sent to the customer.</p>
      {notes?.length === 0 && <p className="mb-3 text-xs text-muted">No notes yet.</p>}
      <ul className="mb-3 space-y-2">
        {notes?.map((n) => (
          <li key={n.id} className="rounded-control bg-warning-soft/60 px-3 py-2 text-sm">
            {editing?.id === n.id ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void act(() => api(`${base}/${n.id}`, { method: "PATCH", body: { body: editing.body } })).then(() => setEditing(null));
                }}
                className="space-y-2"
              >
                <textarea
                  aria-label="Edit note"
                  value={editing.body}
                  maxLength={4000}
                  onChange={(e) => setEditing({ id: n.id, body: e.target.value })}
                  className="w-full rounded-control border-[1.5px] border-line bg-surface px-2 py-1.5 text-sm focus:border-ring focus:outline-none"
                  rows={3}
                />
                <div className="flex gap-2">
                  <Button type="submit" className="!px-3 !py-1 text-xs" loading={busy}>
                    Save
                  </Button>
                  <Button type="button" variant="ghost" className="!px-3 !py-1 text-xs" onClick={() => setEditing(null)}>
                    Cancel
                  </Button>
                </div>
              </form>
            ) : (
              <>
                <p className="whitespace-pre-wrap break-words">{n.body}</p>
                <p className="mt-1 flex flex-wrap items-center gap-x-2 text-[11px] text-muted">
                  <span>
                    {n.author?.name ?? "Former member"} · {formatDateTime(n.created_at)}
                    {n.updated_at !== n.created_at && " · edited"}
                  </span>
                  {n.can_edit && (
                    <button type="button" className="font-semibold text-brand hover:underline" onClick={() => setEditing({ id: n.id, body: n.body })}>
                      Edit
                    </button>
                  )}
                  {n.can_delete && (
                    <button
                      type="button"
                      className="font-semibold text-danger hover:underline"
                      onClick={() => {
                        if (window.confirm("Delete this note?")) void act(() => api(`${base}/${n.id}`, { method: "DELETE" }));
                      }}
                    >
                      Delete
                    </button>
                  )}
                </p>
              </>
            )}
          </li>
        ))}
      </ul>
      {canWrite && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void act(() => api(base, { method: "POST", body: { body: text } })).then(() => setText(""));
          }}
          className="space-y-2"
        >
          <textarea
            aria-label="New note"
            placeholder="Add a note for your team"
            value={text}
            maxLength={4000}
            onChange={(e) => setText(e.target.value)}
            rows={2}
            className="w-full rounded-control border-[1.5px] border-line bg-surface px-3 py-2 text-sm placeholder:text-muted/70 focus:border-ring focus:outline-none dark:bg-surface-muted"
          />
          <Button type="submit" variant="secondary" className="!px-4 !py-1.5 text-xs" loading={busy} disabled={!text.trim()}>
            Add note
          </Button>
        </form>
      )}
      {error && <p className="mt-2 text-xs text-danger">{error}</p>}
    </PanelSection>
  );
}

function HistoryCard({ conversationId, version }: { conversationId: string; version: string }) {
  const [rows, setRows] = useState<HandoverHistory[] | null>(null);

  useEffect(() => {
    api<HandoverHistory[]>(`/conversations/${conversationId}/handovers`).then(setRows, () => setRows([]));
  }, [conversationId, version]);

  if (!rows?.length) return null;
  return (
    <PanelSection title="Handled by people">
      <ul className="space-y-2 text-xs">
        {rows.map((h) => (
          <li key={h.id}>
            <p className="font-medium">{handoverReason(h.reason)}</p>
            <p className="text-muted">
              {formatDateTime(h.started_at)}
              {h.agent ? ` · ${h.agent.name}` : ""}
              {h.released_at
                ? ` · back to AI ${formatDateTime(h.released_at)}${h.released_by ? ` by ${h.released_by.name}` : ""}`
                : " · still with your team"}
            </p>
          </li>
        ))}
      </ul>
    </PanelSection>
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
          {m.sender_type === "ai" && (m.ai_provenance?.model ? ` · AI (${m.ai_provenance.model})` : " · AI")}
        </p>
        {m.sender_type === "ai" && !!m.ai_provenance?.sources?.length && (
          <p className="mt-1 text-xs text-muted">
            Sources:{" "}
            {[...new Set(m.ai_provenance.sources.map((src) => src.title))].join(", ")}
          </p>
        )}
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
    <form onSubmit={send} className="@container space-y-3 border-t border-line px-5 py-4">
      {!windowOpen && (
        <p className="text-xs text-muted">
          More than 24 hours have passed since this customer last wrote, so WhatsApp only allows an approved template.
        </p>
      )}
      <div className="grid gap-3 @lg:grid-cols-[180px_1fr]">
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
            <div className="grid gap-3 @lg:grid-cols-2">
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
