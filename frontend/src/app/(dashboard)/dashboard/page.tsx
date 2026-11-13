"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { change, formatDuration, StatTile } from "@/components/charts/Charts";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { api, errorMessage } from "@/lib/api/client";
import type { AnalyticsOverview, Conversation, InboxCounts, Page, SetupProgress, Subscription } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { contactLabel, handoverReason } from "@/lib/inbox";
import { formatDate, formatDateTime } from "@/lib/locale";

const REFRESH_MS = 30_000;

function Step({ done, title, detail, href, action }: { done: boolean; title: string; detail?: string; href?: string; action?: string }) {
  return (
    <li className="flex items-center justify-between gap-3 py-3">
      <span className="flex min-w-0 items-start gap-3 text-sm">
        <span
          aria-hidden
          className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs ${done ? "bg-brand-gradient text-white" : "border-[1.5px] border-line"}`}
        >
          {done ? "✓" : ""}
        </span>
        <span className="min-w-0">
          <span className={done ? "text-muted line-through" : "font-medium"}>{title}</span>
          <span className="sr-only">{done ? " (done)" : " (to do)"}</span>
          {!done && detail && <span className="block text-xs text-muted">{detail}</span>}
        </span>
      </span>
      {!done && href && (
        <Link href={href} className="shrink-0 text-sm font-medium text-brand hover:underline">
          {action}
        </Link>
      )}
    </li>
  );
}

export default function OverviewPage() {
  const { me, can } = useAuth();
  const [sub, setSub] = useState<Subscription | null>(null);
  const [setup, setSetup] = useState<SetupProgress | null>(null);
  const [counts, setCounts] = useState<InboxCounts | null>(null);
  const [waiting, setWaiting] = useState<Conversation[] | null>(null);
  const [week, setWeek] = useState<AnalyticsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const canBilling = can("billing:read");
  const canInbox = can("conversations:read");
  const canAnalytics = can("analytics:read");
  const tenantId = me?.active_tenant_id;

  useEffect(() => {
    const fail = (e: unknown) => setError(errorMessage(e));
    api<SetupProgress>("/analytics/setup").then(setSetup, fail);
    if (canBilling) api<Subscription>("/billing/subscription").then(setSub, fail);
    if (canAnalytics) api<AnalyticsOverview>("/analytics/overview?days=7").then(setWeek, fail);
  }, [canBilling, canAnalytics, tenantId]);

  useEffect(() => {
    if (!canInbox) return;
    const load = () =>
      Promise.all([
        api<InboxCounts>("/conversations/counts"),
        api<Page<Conversation>>("/conversations?view=needs_human&limit=5"),
      ]).then(
        ([c, page]) => {
          setCounts(c);
          setWaiting(page.items);
        },
        (e) => setError(errorMessage(e)),
      );
    void Promise.resolve().then(load);
    const t = window.setInterval(load, REFRESH_MS);
    return () => window.clearInterval(t);
  }, [canInbox, tenantId]);

  if (!me) return null;
  const tenant = me.memberships.find((m) => m.tenant_id === me.active_tenant_id);

  const steps = setup
    ? [
        {
          done: setup.whatsapp_connected,
          title: "Connect a WhatsApp number",
          detail: "Sign in with Meta to link your WhatsApp Business number.",
          href: can("whatsapp:manage") ? "/whatsapp" : undefined,
          action: "Connect",
        },
        {
          done: setup.assistant_published,
          title: "Set up the AI assistant",
          detail: "Choose a model, write its instructions and publish it.",
          href: can("ai:configure") ? "/ai" : undefined,
          action: "Set up",
        },
        {
          done: setup.knowledge_documents > 0,
          title: "Add your business information",
          detail: "Upload price lists, FAQs or policies so the assistant answers from them.",
          href: can("knowledge:manage") ? "/knowledge" : undefined,
          action: "Add",
        },
        {
          done: setup.active_members > 1,
          title: "Invite a teammate",
          detail: "Bring in the people who will answer customers.",
          href: can("members:invite") ? "/team" : undefined,
          action: "Invite",
        },
        { done: me.user.email_verified, title: "Verify your email address", detail: "Use the banner above to resend the link." },
        { done: me.user.mfa_enabled, title: "Turn on two-factor authentication", href: "/account", action: "Set up" },
      ]
    : [];
  const remaining = steps.filter((s) => !s.done).length;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">Welcome, {me.user.full_name.split(" ")[0]}</h1>
        {tenant && (
          <p className="mt-1 text-sm text-muted">
            {tenant.tenant_name} · <span className="capitalize">{me.role}</span>
          </p>
        )}
      </header>
      {error && <Alert tone="error">{error}</Alert>}

      {canInbox && counts && (
        <section aria-label="Inbox" className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <InboxTile label="Needs a person" value={counts.needs_human} view="needs_human" urgent />
          <InboxTile label="Waiting for a reply" value={counts.awaiting} view="awaiting" urgent />
          <InboxTile label="Unassigned" value={counts.unassigned} view="unassigned" />
          <InboxTile label="Assigned to you" value={counts.mine} view="mine" />
        </section>
      )}

      {canAnalytics && week && (
        <section aria-labelledby="week-heading" className="space-y-3">
          <div className="flex items-end justify-between gap-3">
            <h2 id="week-heading" className="text-base font-semibold">
              Last 7 days
            </h2>
            <Link href="/analytics" className="text-sm font-medium text-brand hover:underline">
              See analytics
            </Link>
          </div>
          <div className="grid grid-cols-[minmax(0,1fr)] gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile
              label="Customer messages"
              value={week.totals.messages_in.toLocaleString()}
              delta={change(week.totals.messages_in, week.previous.messages_in)}
            />
            <StatTile
              label="Handled by the assistant alone"
              value={week.totals.ai_resolution_rate === null ? "—" : `${Math.round(week.totals.ai_resolution_rate * 100)}%`}
              delta={change(week.totals.ai_resolution_rate, week.previous.ai_resolution_rate, { percentPoints: true })}
            />
            <StatTile
              label="Time to a person after handover"
              value={formatDuration(week.response.handover_median_seconds)}
              hint={week.response.handovers ? `Median of ${week.response.handovers} handovers` : "No handovers"}
            />
            <StatTile
              label="Leads won"
              value={week.totals.leads_won.toLocaleString()}
              delta={change(week.totals.leads_won, week.previous.leads_won)}
            />
          </div>
        </section>
      )}
      <div className="grid grid-cols-[minmax(0,1fr)] items-start gap-6 lg:grid-cols-2">
        {canInbox && (
          <Card
            title="Needs a person"
            description="The assistant handed these to your team."
            actions={
              <Link href="/conversations?view=needs_human" className="text-sm font-medium text-brand hover:underline">
                Open inbox
              </Link>
            }
          >
            {waiting === null ? (
              <p className="text-sm text-muted">Loading…</p>
            ) : waiting.length === 0 ? (
              <EmptyState title="Nobody is waiting for a person" description="When the assistant hands a conversation over, it shows here." />
            ) : (
              <ul className="divide-y divide-line">
                {waiting.map((c) => (
                  <li key={c.id}>
                    <Link href={`/conversations?c=${c.id}`} className="flex items-start justify-between gap-3 py-3 hover:text-brand">
                      <span className="min-w-0">
                        <span className="block truncate text-sm font-semibold">{contactLabel(c.contact)}</span>
                        <span className="block truncate text-xs text-muted">
                          {c.handover ? handoverReason(c.handover.reason) : (c.last_message_preview ?? "")}
                        </span>
                      </span>
                      <span className="shrink-0 text-xs text-muted">{formatDateTime(c.last_message_at)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        )}

        {setup && remaining > 0 && (
          <Card title="Get set up" description={`${steps.length - remaining} of ${steps.length} done. Finish these to start answering customers.`}>
            <ul className="divide-y divide-line">
              {steps.map((s) => (
                <Step key={s.title} {...s} />
              ))}
            </ul>
          </Card>
        )}

        {canBilling && (
          <Card title="Plan">
            {sub ? (
              <dl className="space-y-3 text-sm">
                <div className="flex justify-between">
                  <dt className="text-muted">Plan</dt>
                  <dd className="flex items-center gap-2 font-medium">
                    {sub.plan_name ?? "None"}
                    {sub.status && <Badge tone={sub.status === "active" ? "success" : "brand"}>{sub.status}</Badge>}
                  </dd>
                </div>
                {sub.trial_ends_at && (
                  <div className="flex justify-between">
                    <dt className="text-muted">Trial ends</dt>
                    <dd>{formatDate(sub.trial_ends_at)}</dd>
                  </div>
                )}
                <div className="flex justify-between">
                  <dt className="text-muted">Team seats</dt>
                  <dd>
                    {sub.seats_used} of {sub.seats_included}
                  </dd>
                </div>
              </dl>
            ) : (
              !error && <p className="text-sm text-muted">Loading plan…</p>
            )}
          </Card>
        )}
      </div>

    </div>
  );
}

function InboxTile({ label, value, view, urgent }: { label: string; value: number; view: string; urgent?: boolean }) {
  return (
    <Link
      href={`/conversations?view=${view}`}
      className="rounded-card border border-line bg-surface p-4 shadow-card transition-colors hover:border-ring"
    >
      <p className="text-sm text-muted">{label}</p>
      <p className={`mt-1 text-2xl font-semibold ${urgent && value > 0 ? "text-warning" : ""}`}>{value.toLocaleString()}</p>
    </Link>
  );
}
