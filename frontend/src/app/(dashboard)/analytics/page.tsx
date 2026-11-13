"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  BarList,
  change,
  ChartFrame,
  DataTable,
  dayLabel,
  formatDuration,
  Heatmap,
  hourLabel,
  Legend,
  LineChart,
  StatTile,
  WEEKDAYS,
  type Series,
} from "@/components/charts/Charts";
import { Alert } from "@/components/ui/Alert";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type { AnalyticsOverview, AnalyticsPeriod } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatMoney, handoverReason } from "@/lib/inbox";

const PERIODS: { value: AnalyticsPeriod; label: string }[] = [
  { value: 7, label: "Last 7 days" },
  { value: 30, label: "Last 30 days" },
  { value: 90, label: "Last 90 days" },
];

type MessageKey = "customer" | "ai" | "team";
const MESSAGE_SERIES: Series<MessageKey>[] = [
  { key: "customer", label: "From customers", color: "var(--color-series-1)" },
  { key: "ai", label: "Sent by the assistant", color: "var(--color-series-2)" },
  { key: "team", label: "Sent by your team", color: "var(--color-series-3)" },
];

const PURPOSES: Record<string, string> = {
  auto_reply: "Replies to customers",
  summary: "Summaries for the team",
  preview: "Previews on the AI page",
  test: "Connection tests",
  knowledge_index: "Indexing your documents",
  retrieval: "Finding knowledge for replies",
  knowledge_reindex: "Re-indexing after a model change",
  search_test: "Knowledge search tests",
};

function percent(rate: number | null): string {
  return rate === null ? "—" : `${Math.round(rate * 100)}%`;
}

export default function AnalyticsPage() {
  const { can } = useAuth();
  const [days, setDays] = useState<AnalyticsPeriod>(30);
  const [data, setData] = useState<AnalyticsOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const allowed = can("analytics:read");

  useEffect(() => {
    if (!allowed) return;
    let live = true;
    void Promise.resolve()
      .then(() => setLoading(true))
      .then(() => api<AnalyticsOverview>(`/analytics/overview?days=${days}`))
      .then(
        (d) => {
          if (!live) return;
          setData(d);
          setError(null);
        },
        (err) => live && setError(errorMessage(err)),
      )
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [days, allowed]);

  if (!allowed) {
    return (
      <EmptyState
        title="Analytics are for supervisors and admins"
        description="Ask an owner or admin of this business if you need access."
      />
    );
  }
  if (!data && !error) return <PageLoader label="Loading analytics" />;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Analytics</h1>
          <p className="mt-1 text-sm text-muted">
            How your WhatsApp inbox, assistant and team are doing.
            {data && ` Days and hours are in ${data.timezone.replace(/_/g, " ")}.`}
          </p>
        </div>
        <div role="radiogroup" aria-label="Period" className="flex rounded-full border border-line bg-surface p-1">
          {PERIODS.map((p) => (
            <button
              key={p.value}
              type="button"
              role="radio"
              aria-checked={days === p.value}
              onClick={() => setDays(p.value)}
              className={`rounded-full px-3 py-1.5 text-sm font-medium ${days === p.value ? "bg-brand-soft text-brand-ink" : "text-ink-2 hover:text-brand"}`}
            >
              {p.label}
            </button>
          ))}
        </div>
      </header>
      {error && <Alert tone="error">{error}</Alert>}
      {data && <Report data={data} dimmed={loading} />}
    </div>
  );
}

function Report({ data, dimmed }: { data: AnalyticsOverview; dimmed: boolean }) {
  const t = data.totals;
  const p = data.previous;
  const r = data.response;
  const quiet = t.messages_in + t.messages_ai + t.messages_team === 0;

  return (
    <div className={`space-y-6 transition-opacity ${dimmed ? "opacity-60" : ""}`} aria-busy={dimmed}>
      <div className="grid grid-cols-[minmax(0,1fr)] gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Customer messages"
          value={t.messages_in.toLocaleString()}
          delta={change(t.messages_in, p.messages_in)}
          hint={`${t.conversations_started.toLocaleString()} new conversation${t.conversations_started === 1 ? "" : "s"}`}
        />
        <StatTile
          label="Handled by the assistant alone"
          value={percent(t.ai_resolution_rate)}
          delta={change(t.ai_resolution_rate, p.ai_resolution_rate, { percentPoints: true })}
          hint={
            t.ai_conversations
              ? `Share of the ${t.ai_conversations} conversation${t.ai_conversations === 1 ? "" : "s"} it answered that never needed a person`
              : "The assistant hasn't answered anyone in this period"
          }
        />
        <StatTile
          label="Time to a person after handover"
          value={formatDuration(r.handover_median_seconds)}
          hint={
            r.handovers
              ? `Median · ${r.handovers_answered} of ${r.handovers} handovers answered`
              : "No handovers in this period"
          }
        />
        <StatTile
          label="Leads won"
          value={t.leads_won.toLocaleString()}
          delta={change(t.leads_won, p.leads_won)}
          hint={
            data.won_value.length
              ? data.won_value.map((v) => formatMoney(v.total, v.currency)).join(" · ")
              : `${t.leads_created.toLocaleString()} new lead${t.leads_created === 1 ? "" : "s"}`
          }
        />
      </div>

      {quiet ? (
        <Card>
          <EmptyState
            title="No messages in this period"
            description="Charts appear once customers write to your WhatsApp numbers. Try a longer period, or check the WhatsApp page if you expected messages."
          />
        </Card>
      ) : (
        <>
          <ChartFrame
            title="Messages per day"
            description="Every message in and out of your WhatsApp numbers, by who wrote it."
            legend={<Legend series={MESSAGE_SERIES} />}
            table={
              <DataTable
                head={["Day", ...MESSAGE_SERIES.map((s) => s.label)]}
                rows={data.daily.map((d) => [dayLabel(d.date, { weekday: "short", day: "numeric", month: "short" }), d.customer, d.ai, d.team])}
              />
            }
          >
            <LineChart
              points={data.daily.map((d) => ({ x: d.date, customer: d.customer, ai: d.ai, team: d.team }))}
              series={MESSAGE_SERIES}
              xLabel={(x) => dayLabel(x)}
            />
          </ChartFrame>

          <div className="grid grid-cols-[minmax(0,1fr)] items-start gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <ChartFrame
              title="When customers write"
              description="Customer messages by day of the week and hour."
              table={
                <DataTable
                  head={["Day and hour", "Messages"]}
                  rows={[...data.hours]
                    .sort((a, b) => a.weekday - b.weekday || a.hour - b.hour)
                    .map((h) => [`${WEEKDAYS[h.weekday - 1]} ${hourLabel(h.hour)}`, h.messages])}
                />
              }
            >
              <Heatmap cells={data.hours} />
            </ChartFrame>
            <ChartFrame
              title="Why the assistant handed over"
              description={`${t.ai_handovers.toLocaleString()} handover${t.ai_handovers === 1 ? "" : "s"} to your team.`}
              table={
                <DataTable head={["Reason", "Handovers"]} rows={data.handover_reasons.map((h) => [handoverReason(h.reason), h.count])} />
              }
            >
              {data.handover_reasons.length ? (
                <BarList items={data.handover_reasons.map((h) => ({ label: handoverReason(h.reason), value: h.count }))} />
              ) : (
                <EmptyState title="No handovers" description="The assistant answered everything it was asked." />
              )}
              {data.open_unanswered > 0 && (
                <p className="mt-4 text-sm text-muted">
                  {data.open_unanswered} question{data.open_unanswered === 1 ? "" : "s"} still need an answer in your knowledge base.{" "}
                  <Link href="/knowledge" className="font-medium text-brand hover:underline">
                    Review them
                  </Link>
                </p>
              )}
            </ChartFrame>
          </div>
        </>
      )}

      <div className="grid grid-cols-[minmax(0,1fr)] items-start gap-6 xl:grid-cols-2">
        <Card title="First reply to new conversations" description="From the customer's first message to the first answer.">
          <dl className="grid grid-cols-[minmax(0,1fr)] gap-4 sm:grid-cols-3">
            <ReplyStat label="By the assistant" value={r.first_reply_ai} />
            <ReplyStat label="By your team" value={r.first_reply_team} />
            <div>
              <dt className="text-sm text-muted">Not answered yet</dt>
              <dd className="mt-1 text-2xl font-semibold">{r.first_reply_none.toLocaleString()}</dd>
              <dd className="text-xs text-muted">new conversations</dd>
            </div>
          </dl>
        </Card>
        <DeliveryCard delivery={data.delivery} />
        {data.ai_cost && <CostCard cost={data.ai_cost} />}
      </div>

      <Card title="Team" description="People who replied to customers or picked up a handover in this period.">
        {data.agents.length === 0 ? (
          <EmptyState title="No replies from the team yet" description="Replies and handovers your team handles will show here." />
        ) : (
          <div className="-mx-6 overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead className="text-left text-xs text-muted">
                <tr className="border-b border-line">
                  <th className="px-6 py-2 font-medium">Person</th>
                  <th className="px-3 py-2 text-right font-medium">Messages sent</th>
                  <th className="px-3 py-2 text-right font-medium">Conversations</th>
                  <th className="px-3 py-2 text-right font-medium">Handovers</th>
                  <th className="px-3 py-2 text-right font-medium">Median time to reply</th>
                  <th className="px-6 py-2 text-right font-medium">Leads won</th>
                </tr>
              </thead>
              <tbody className="tabular-nums">
                {data.agents.map((a) => (
                  <tr key={a.user_id} className="border-b border-line last:border-0">
                    <td className="px-6 py-3">
                      <p className="font-semibold">{a.name}</p>
                      <p className="text-xs text-muted">{a.role ? a.role[0].toUpperCase() + a.role.slice(1) : "No longer a member"}</p>
                    </td>
                    <td className="px-3 py-3 text-right">{a.messages.toLocaleString()}</td>
                    <td className="px-3 py-3 text-right">{a.conversations.toLocaleString()}</td>
                    <td className="px-3 py-3 text-right">
                      {a.handovers ? `${a.handovers_answered} of ${a.handovers} answered` : "—"}
                    </td>
                    <td className="px-3 py-3 text-right">{formatDuration(a.handover_median_seconds)}</td>
                    <td className="px-6 py-3 text-right">{a.leads_won.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function ReplyStat({ label, value }: { label: string; value: AnalyticsOverview["response"]["first_reply_ai"] }) {
  return (
    <div>
      <dt className="text-sm text-muted">{label}</dt>
      <dd className="mt-1 text-2xl font-semibold">{formatDuration(value?.median_seconds)}</dd>
      <dd className="text-xs text-muted">
        {value ? `Median of ${value.conversations} · slowest 10% over ${formatDuration(value.p90_seconds)}` : "No first replies"}
      </dd>
    </div>
  );
}

function CostCard({ cost }: { cost: NonNullable<AnalyticsOverview["ai_cost"]> }) {
  return (
    <Card
      title="AI usage"
      description="Requests to AI models in this period and their estimated cost."
      actions={
        <Link href="/ai" className="text-sm font-medium text-brand hover:underline">
          Usage by model
        </Link>
      }
    >
      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 sm:grid-cols-2">
        <div>
          <p className="text-sm text-muted">Estimated cost</p>
          <p className="mt-1 text-2xl font-semibold">
            {cost.cost.length ? cost.cost.map((c) => formatMoney(c.total, c.currency)).join(" + ") : "—"}
          </p>
          <p className="text-xs text-muted">
            {cost.calls.toLocaleString()} request{cost.calls === 1 ? "" : "s"}
            {cost.unpriced_calls ? ` · ${cost.unpriced_calls} without a price in the catalogue` : ""}
          </p>
        </div>
        {cost.calls_by_purpose.length > 0 && (
          <BarList
            items={cost.calls_by_purpose.map((c) => ({ label: PURPOSES[c.purpose] ?? c.purpose, value: c.calls }))}
          />
        )}
      </div>
    </Card>
  );
}

const CATEGORIES: Record<string, string> = {
  service: "Service (customer-started)",
  marketing: "Marketing",
  utility: "Utility",
  authentication: "Authentication",
  marketing_lite: "Marketing lite",
};

function DeliveryCard({ delivery: d }: { delivery: AnalyticsOverview["delivery"] }) {
  const total = d.sent + d.delivered + d.read + d.failed + d.pending;
  const share = (n: number) => (total ? `${Math.round((n / total) * 100)}%` : "—");
  const rows = [
    { label: "Read", value: d.read },
    { label: "Delivered, not read yet", value: d.delivered },
    { label: "Sent to the phone network", value: d.sent },
    { label: "Waiting for Meta", value: d.pending },
    { label: "Failed", value: d.failed },
  ];
  return (
    <Card title="Message delivery" description="What happened to the messages you and the assistant sent, as reported by WhatsApp.">
      {total === 0 ? (
        <EmptyState title="Nothing sent in this period" />
      ) : (
        <div className="space-y-5">
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-[minmax(0,1fr)_auto_auto]">
            {rows.map((r) => (
              <div key={r.label} className="contents">
                <dt className="text-ink-2 sm:col-start-1">{r.label}</dt>
                <dd className="text-right font-semibold tabular-nums">{r.value.toLocaleString()}</dd>
                <dd className="hidden text-right text-muted tabular-nums sm:block">{share(r.value)}</dd>
              </div>
            ))}
          </dl>
          {d.top_errors.length > 0 && (
            <div>
              <p className="text-sm font-medium">Most common failures</p>
              <ul className="mt-1 space-y-0.5 text-sm text-muted">
                {d.top_errors.map((e) => (
                  <li key={e.code}>
                    WhatsApp error {e.code}: {e.count.toLocaleString()}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div>
            <p className="text-sm font-medium">Charged by Meta</p>
            {d.billable.length === 0 ? (
              <p className="mt-1 text-sm text-muted">WhatsApp hasn&apos;t marked any of these messages as billable.</p>
            ) : (
              <ul className="mt-1 space-y-0.5 text-sm text-muted">
                {d.billable.map((b) => (
                  <li key={b.category}>
                    {CATEGORIES[b.category] ?? b.category}: {b.messages.toLocaleString()} message{b.messages === 1 ? "" : "s"}
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-1 text-xs text-muted">Meta bills your WhatsApp Business account directly at its published rates.</p>
          </div>
        </div>
      )}
    </Card>
  );
}
