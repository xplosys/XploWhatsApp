"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type {
  BillingInterval,
  BillingPlan,
  BillingPlans,
  BillingProfile,
  BillingSubscription,
  BillingTransaction,
  BillingUsage,
  CheckoutStart,
  GstState,
  InvoiceSummary,
  Tenant,
} from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { errorDetails, formatMoney, payWithRazorpay, priceLabel, taxLabel } from "@/lib/billing";
import { formatDate } from "@/lib/locale";

type Problem = { message: string; details: string[] };

const toProblem = (err: unknown): Problem => ({
  message: err instanceof Error && !("status" in err) ? err.message : errorMessage(err),
  details: errorDetails(err),
});

function ProblemAlert({ problem }: { problem: Problem }) {
  return (
    <Alert tone="error">
      {problem.message}
      {problem.details.length > 0 && (
        <ul className="mt-2 list-disc pl-5">
          {problem.details.map((d) => (
            <li key={d}>{d}</li>
          ))}
        </ul>
      )}
    </Alert>
  );
}

const STATUS: Record<string, { label: string; tone: "success" | "warning" | "danger" | "brand" | "neutral" }> = {
  trialing: { label: "Free trial", tone: "brand" },
  active: { label: "Active", tone: "success" },
  past_due: { label: "Payment due", tone: "warning" },
};

// ---- plan status -----------------------------------------------------------------------

function PlanStatus({
  sub,
  manage,
  onChange,
}: {
  sub: BillingSubscription;
  manage: boolean;
  onChange: (s: BillingSubscription) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);

  async function act(name: string, path: string, method: "POST" | "DELETE") {
    setBusy(name);
    setProblem(null);
    try {
      onChange(await api<BillingSubscription>(path, { method }));
      setConfirmCancel(false);
    } catch (err) {
      setProblem(toProblem(err));
    } finally {
      setBusy(null);
    }
  }

  const status = sub.status && sub.in_force ? STATUS[sub.status] : null;
  const paid = sub.status !== "trialing" && sub.gateway !== null && sub.in_force;

  return (
    <Card
      title="Your plan"
      actions={status ? <Badge tone={status.tone}>{status.label}</Badge> : <Badge tone="danger">No plan in force</Badge>}
    >
      {problem && (
        <div className="mb-4">
          <ProblemAlert problem={problem} />
        </div>
      )}
      {sub.in_force && sub.plan_name ? (
        <div className="space-y-3 text-sm">
          <p className="text-2xl font-semibold">
            {sub.plan_name}
            {sub.price && <span className="ml-2 text-base font-medium text-muted">{priceLabel(sub.price)}</span>}
          </p>
          {sub.status === "trialing" && (
            <p className="text-ink-2">
              Your trial ends on <strong>{formatDate(sub.trial_ends_at)}</strong>. Choose a plan below to keep the AI
              assistant answering after that.
            </p>
          )}
          {sub.status === "active" && !sub.cancel_at_period_end && sub.current_period_end && (
            <p className="text-ink-2">
              Renews on <strong>{formatDate(sub.current_period_end)}</strong>
              {sub.gateway && <> through {sub.gateway === "razorpay" ? "Razorpay" : "Stripe"}</>}.
            </p>
          )}
          {sub.status === "past_due" && (
            <Alert tone="warning">
              Your latest payment didn&apos;t go through. The payment provider will retry; update your card or UPI
              mandate there. If it isn&apos;t paid by <strong>{formatDate(sub.grace_ends_at)}</strong>, the plan stops
              working.
            </Alert>
          )}
          {sub.cancel_at_period_end && (
            <Alert tone="warning">
              Your plan ends on <strong>{formatDate(sub.current_period_end)}</strong> and won&apos;t renew.
              {manage && sub.gateway === "stripe" && (
                <Button variant="secondary" className="ml-3 py-1" loading={busy === "resume"} onClick={() => act("resume", "/billing/subscription/resume", "POST")}>
                  Keep my plan
                </Button>
              )}
            </Alert>
          )}
          {sub.pending_price && (
            <Alert tone="info">
              Switching to <strong>{sub.pending_plan_name}</strong> ({priceLabel(sub.pending_price)}) on{" "}
              {formatDate(sub.current_period_end)}.
              {manage && (
                <Button variant="secondary" className="ml-3 py-1" loading={busy === "undo"} onClick={() => act("undo", "/billing/subscription/pending-change", "DELETE")}>
                  Stay on {sub.plan_name}
                </Button>
              )}
            </Alert>
          )}
          <p className="text-muted">
            {sub.seats_used} of {sub.seats_included} team seats in use
            {sub.extra_seats > 0 && ` (${sub.extra_seats} extra)`}.
          </p>
          {manage && paid && !sub.cancel_at_period_end && (
            <div className="pt-2">
              {confirmCancel ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span>
                    Cancel at the end of this period?{" "}
                    {sub.gateway === "razorpay" && "With Razorpay this can't be undone; you'd choose a plan again later."}
                  </span>
                  <Button variant="danger" loading={busy === "cancel"} onClick={() => act("cancel", "/billing/subscription/cancel", "POST")}>
                    Yes, cancel
                  </Button>
                  <Button variant="ghost" onClick={() => setConfirmCancel(false)}>
                    Keep plan
                  </Button>
                </div>
              ) : (
                <Button variant="ghost" className="-ml-2 px-2" onClick={() => setConfirmCancel(true)}>
                  Cancel plan
                </Button>
              )}
            </div>
          )}
        </div>
      ) : (
        <p className="text-sm text-ink-2">
          {sub.status === null
            ? "Your trial or plan has ended, so the AI assistant has stopped replying. Your data is kept; choose a plan to carry on."
            : "Your plan isn't in force right now. Choose a plan to carry on."}
        </p>
      )}
    </Card>
  );
}

// ---- usage -------------------------------------------------------------------------------

function UsageMeters({ usage }: { usage: BillingUsage[] }) {
  return (
    <Card title="Usage" description="What your business uses now against the plan's limits.">
      <ul className="grid gap-5 md:grid-cols-2">
        {usage.map((u) => {
          const share = u.limit > 0 ? Math.min(1, u.used / u.limit) : u.used > 0 ? 1 : 0;
          const full = u.used >= u.limit;
          return (
            <li key={u.key}>
              <div className="flex items-baseline justify-between text-sm">
                <span className="font-medium text-ink">{u.label}</span>
                <span className="tabular-nums text-ink-2">
                  {u.used.toLocaleString()} / {u.limit.toLocaleString()}
                </span>
              </div>
              <div
                role="meter"
                aria-label={u.label}
                aria-valuemin={0}
                aria-valuemax={u.limit}
                aria-valuenow={Math.min(u.used, u.limit)}
                className="mt-1.5 h-2 overflow-hidden rounded-full bg-surface-muted"
              >
                <div
                  className={`h-full rounded-full ${full ? "bg-danger" : "bg-series-1"}`}
                  style={{ width: `${Math.max(share * 100, u.used > 0 ? 2 : 0)}%` }}
                />
              </div>
              {full && u.limit >= 0 && (
                <p className="mt-1 text-xs text-danger">{u.limit === 0 ? "Not included in your plan right now" : "Limit reached"}</p>
              )}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

// ---- plans -------------------------------------------------------------------------------

const LIMIT_TEXT: Record<string, (n: number) => string> = {
  max_users: (n) => `${n} team member${n === 1 ? "" : "s"} included`,
  max_whatsapp_numbers: (n) => `${n} WhatsApp number${n === 1 ? "" : "s"}`,
  max_knowledge_documents: (n) => `${n.toLocaleString()} knowledge documents`,
  max_storage_mb: (n) => (n >= 1024 ? `${(n / 1024).toFixed(n % 1024 ? 1 : 0)} GB` : `${n} MB`) + " knowledge storage",
  monthly_ai_messages: (n) => `${n.toLocaleString()} AI replies a month`,
};

function PlanPicker({
  catalogue,
  sub,
  manage,
  onCheckout,
  onChanged,
}: {
  catalogue: BillingPlans;
  sub: BillingSubscription;
  manage: boolean;
  onCheckout: (priceId: string, seats: number) => Promise<void>;
  onChanged: (s: BillingSubscription, immediate: boolean) => void;
}) {
  const [interval, setBillingInterval] = useState<BillingInterval>(sub.price?.interval ?? "month");
  const [currency, setCurrency] = useState(sub.price?.currency ?? catalogue.currencies[0]);
  const [seats, setSeats] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const paid = sub.in_force && sub.gateway !== null && sub.status !== "trialing";
  const offered = (plan: BillingPlan) => plan.prices.find((p) => p.interval === interval && p.currency === currency);
  const hasYearly = catalogue.plans.some((p) => p.prices.some((x) => x.interval === "year" && x.currency === currency));

  async function choose(priceId: string) {
    setBusy(priceId);
    setProblem(null);
    try {
      if (paid) {
        const r = await api<{ immediate: boolean; subscription: BillingSubscription }>("/billing/subscription/change", {
          method: "POST",
          body: { price_id: priceId },
        });
        onChanged(r.subscription, r.immediate);
      } else {
        await onCheckout(priceId, seats);
      }
    } catch (err) {
      setProblem(toProblem(err));
    } finally {
      setBusy(null);
    }
  }

  if (catalogue.plans.length === 0) {
    return (
      <Card title="Plans">
        <EmptyState title="No plans are on sale yet" description="Please contact XploSys support to subscribe." />
      </Card>
    );
  }

  return (
    <Card
      title={paid ? "Change plan" : "Choose a plan"}
      description={
        paid
          ? "Bigger plans start now and you pay the difference for the rest of the period. Smaller plans start when the current period ends."
          : "Prices include tax where it applies. You can change or cancel later."
      }
      actions={
        <div className="flex flex-wrap items-center gap-2">
          {catalogue.currencies.length > 1 && !paid && (
            <select
              aria-label="Currency"
              className="rounded-control border-[1.5px] border-line bg-surface px-3 py-1.5 text-sm"
              value={currency}
              onChange={(e) => setCurrency(e.target.value)}
            >
              {catalogue.currencies.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          )}
          {hasYearly && (
            <div role="radiogroup" aria-label="Billing interval" className="inline-flex rounded-full bg-surface-muted p-1 text-sm">
              {(["month", "year"] as const).map((i) => (
                <button
                  key={i}
                  role="radio"
                  aria-checked={interval === i}
                  onClick={() => setBillingInterval(i)}
                  className={`rounded-full px-4 py-1.5 font-semibold ${interval === i ? "bg-surface text-ink shadow-card" : "text-muted"}`}
                >
                  {i === "month" ? "Monthly" : "Yearly"}
                </button>
              ))}
            </div>
          )}
        </div>
      }
    >
      {!catalogue.payments_ready && <Alert tone="warning">Online payments aren&apos;t switched on yet. Please contact support to subscribe.</Alert>}
      {problem && (
        <div className="mb-4">
          <ProblemAlert problem={problem} />
        </div>
      )}
      <div className="grid gap-4 md:grid-cols-3">
        {catalogue.plans.map((plan) => {
          const price = offered(plan);
          const current = sub.price?.id === price?.id && paid;
          return (
            <div key={plan.id} className={`flex flex-col rounded-card border p-5 ${current ? "border-ring bg-brand-soft/40" : "border-line"}`}>
              <h3 className="text-lg font-semibold">{plan.name}</h3>
              {plan.description && <p className="mt-1 text-sm text-muted">{plan.description}</p>}
              {price ? (
                <>
                  <p className="mt-4 text-2xl font-semibold tabular-nums">{formatMoney(price.amount_with_tax, price.currency)}</p>
                  <p className="text-xs text-muted">
                    per {price.interval}, {taxLabel(price)}
                  </p>
                </>
              ) : (
                <p className="mt-4 text-sm text-muted">Not offered {interval === "month" ? "monthly" : "yearly"}.</p>
              )}
              <ul className="mt-4 flex-1 space-y-1.5 text-sm text-ink-2">
                {Object.entries(plan.limits)
                  .filter(([k]) => LIMIT_TEXT[k])
                  .map(([k, v]) => (
                    <li key={k}>{LIMIT_TEXT[k](v)}</li>
                  ))}
                {plan.features.map((f) => (
                  <li key={f}>{f}</li>
                ))}
                {price?.seat_amount && (
                  <li>
                    Extra seats {formatMoney((Number(price.seat_amount) * (1 + Number(price.tax_rate) / 100)).toFixed(2), price.currency)} each
                  </li>
                )}
              </ul>
              {manage && price && (
                <div className="mt-5 space-y-3">
                  {!paid && price.seat_amount && (
                    <TextField
                      label="Extra seats"
                      type="number"
                      min={0}
                      max={500}
                      value={seats}
                      onChange={(e) => setSeats(Math.max(0, Math.min(500, Number(e.target.value) || 0)))}
                    />
                  )}
                  {current ? (
                    <Badge tone="brand">Your plan</Badge>
                  ) : (
                    <Button
                      className="w-full"
                      loading={busy === price.id}
                      disabled={!catalogue.payments_ready || busy !== null || sub.cancel_at_period_end || sub.status === "past_due"}
                      onClick={() => choose(price.id)}
                    >
                      {paid ? `Switch to ${plan.name}` : `Choose ${plan.name}`}
                    </Button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}

// ---- seats -------------------------------------------------------------------------------

function Seats({ sub, onChange }: { sub: BillingSubscription; onChange: (s: BillingSubscription) => void }) {
  const [value, setValue] = useState(sub.extra_seats);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [saved, setSaved] = useState(false);
  const price = sub.price;
  if (!price?.seat_amount) return null;
  const each = (Number(price.seat_amount) * (1 + Number(price.tax_rate) / 100)).toFixed(2);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setProblem(null);
    setSaved(false);
    try {
      onChange(await api<BillingSubscription>("/billing/subscription/seats", { method: "PUT", body: { extra_seats: value } }));
      setSaved(true);
    } catch (err) {
      setProblem(toProblem(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title="Extra seats"
      description={`${formatMoney(each, price.currency)} per seat per ${price.interval}. Added seats are charged for the rest of this period; removed seats stop at the next renewal.`}
    >
      {problem && (
        <div className="mb-4">
          <ProblemAlert problem={problem} />
        </div>
      )}
      {saved && (
        <div className="mb-4">
          <Alert tone="success">Seats updated.</Alert>
        </div>
      )}
      <form onSubmit={save} className="flex flex-wrap items-end gap-3">
        <div className="w-40">
          <TextField label="Extra seats" type="number" min={0} max={500} value={value} onChange={(e) => setValue(Math.max(0, Number(e.target.value) || 0))} />
        </div>
        <Button type="submit" loading={busy} disabled={value === sub.extra_seats}>
          Save seats
        </Button>
      </form>
    </Card>
  );
}

// ---- billing details ---------------------------------------------------------------------

function BillingDetails({
  profile,
  country,
  manage,
  onSaved,
}: {
  profile: BillingProfile | null;
  country: string;
  manage: boolean;
  onSaved: (p: BillingProfile) => void;
}) {
  const india = country === "IN";
  const [states, setStates] = useState<GstState[]>([]);
  const [form, setForm] = useState({
    legal_name: profile?.legal_name ?? "",
    email: profile?.email ?? "",
    address_line1: profile?.address_line1 ?? "",
    address_line2: profile?.address_line2 ?? "",
    city: profile?.city ?? "",
    state_code: profile?.state_code ?? "",
    state: profile?.state ?? "",
    postal_code: profile?.postal_code ?? "",
    gstin: profile?.gstin ?? "",
    tax_id: profile?.tax_id ?? "",
  });
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (india) api<GstState[]>("/billing/states").then(setStates).catch(() => setStates([]));
  }, [india]);

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    setSaved(false);
    setForm((f) => ({ ...f, [k]: e.target.value }));
  };

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const body = Object.fromEntries(Object.entries(form).map(([k, v]) => [k, v.trim() || null]));
      const saved = await api<BillingProfile>("/billing/profile", { method: "PUT", body: { ...body, legal_name: form.legal_name } });
      // The API fills the state from the GSTIN and normalises it.
      setForm((f) => ({ ...f, state_code: saved.state_code ?? "", state: saved.state ?? "", gstin: saved.gstin ?? "" }));
      onSaved(saved);
      setSaved(true);
    } catch (err) {
      setProblem(toProblem(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title="Billing details"
      description={india ? "Shown on your tax invoices. Add your GSTIN to claim input tax credit." : "Shown on your invoices."}
    >
      {problem && (
        <div className="mb-4">
          <ProblemAlert problem={problem} />
        </div>
      )}
      {saved && (
        <div className="mb-4">
          <Alert tone="success">Billing details saved.</Alert>
        </div>
      )}
      <form onSubmit={save} className="grid gap-4 md:grid-cols-2">
        <TextField label="Legal name" value={form.legal_name} onChange={set("legal_name")} required minLength={2} disabled={!manage} />
        <TextField label="Billing email" type="email" value={form.email} onChange={set("email")} hint="Invoices and payment notices also go here." disabled={!manage} />
        <TextField label="Address" value={form.address_line1} onChange={set("address_line1")} disabled={!manage} />
        <TextField label="Address line 2" value={form.address_line2} onChange={set("address_line2")} disabled={!manage} />
        <TextField label="City" value={form.city} onChange={set("city")} disabled={!manage} />
        {india ? (
          <SelectField
            label="State"
            value={form.state_code}
            onChange={set("state_code")}
            disabled={!manage}
            options={[{ value: "", label: "Select your state" }, ...states.map((s) => ({ value: s.code, label: s.name }))]}
          />
        ) : (
          <TextField label="State or region" value={form.state} onChange={set("state")} disabled={!manage} />
        )}
        <TextField label={india ? "PIN code" : "Postal code"} value={form.postal_code} onChange={set("postal_code")} disabled={!manage} />
        {india ? (
          <TextField
            label="GSTIN (optional)"
            value={form.gstin}
            onChange={set("gstin")}
            maxLength={15}
            hint="If you add one, the state is taken from it."
            disabled={!manage}
          />
        ) : (
          <TextField label="Tax ID (optional)" value={form.tax_id} onChange={set("tax_id")} hint="VAT or EIN, shown on invoices." disabled={!manage} />
        )}
        {manage && (
          <div className="md:col-span-2">
            <Button type="submit" loading={busy}>
              Save billing details
            </Button>
          </div>
        )}
      </form>
    </Card>
  );
}

// ---- history -----------------------------------------------------------------------------

function History({ invoices, payments }: { invoices: InvoiceSummary[]; payments: BillingTransaction[] }) {
  return (
    <>
      <Card title="Invoices" description="A tax invoice is issued for each successful payment.">
        {invoices.length === 0 ? (
          <EmptyState title="No invoices yet" description="Your first invoice appears after your first payment." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-muted">
                <tr>
                  <th className="py-2 pr-4 font-medium">Invoice</th>
                  <th className="py-2 pr-4 font-medium">Date</th>
                  <th className="py-2 pr-4 font-medium">Period</th>
                  <th className="py-2 pr-4 text-right font-medium">Total</th>
                  <th className="py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {invoices.map((i) => (
                  <tr key={i.id}>
                    <td className="py-2.5 pr-4 font-medium">{i.number}</td>
                    <td className="py-2.5 pr-4">{formatDate(i.issued_at)}</td>
                    <td className="py-2.5 pr-4 text-muted">
                      {i.period_start ? `${formatDate(i.period_start)} to ${formatDate(i.period_end)}` : "—"}
                    </td>
                    <td className="py-2.5 pr-4 text-right tabular-nums">{formatMoney(i.total, i.currency)}</td>
                    <td className="py-2.5 text-right">
                      <Link href={`/billing/invoices/${i.id}`} className="font-semibold text-brand hover:underline">
                        View
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {payments.length > 0 && (
        <Card title="Payments">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-muted">
                <tr>
                  <th className="py-2 pr-4 font-medium">Date</th>
                  <th className="py-2 pr-4 font-medium">Status</th>
                  <th className="py-2 pr-4 font-medium">Method</th>
                  <th className="py-2 text-right font-medium">Amount</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {payments.map((t) => (
                  <tr key={t.id}>
                    <td className="py-2.5 pr-4">{formatDate(t.occurred_at)}</td>
                    <td className="py-2.5 pr-4">
                      <Badge tone={t.status === "succeeded" ? "success" : t.status === "failed" ? "danger" : "neutral"}>
                        {t.status === "succeeded" ? "Paid" : t.status}
                      </Badge>
                      {t.failure_reason && <span className="ml-2 text-xs text-muted">{t.failure_reason}</span>}
                    </td>
                    <td className="py-2.5 pr-4 uppercase text-muted">{t.method ?? "—"}</td>
                    <td className="py-2.5 text-right tabular-nums">{formatMoney(t.amount, t.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}

// ---- page --------------------------------------------------------------------------------

interface Data {
  sub: BillingSubscription;
  usage: BillingUsage[];
  catalogue: BillingPlans;
  profile: BillingProfile | null;
  tenant: Tenant;
  invoices: InvoiceSummary[];
  payments: BillingTransaction[];
}

function Billing() {
  const { can } = useAuth();
  const manage = can("billing:manage");
  const params = useSearchParams();
  const router = useRouter();
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const confirmed = useRef(false);

  const load = useCallback(async () => {
    const [sub, usage, catalogue, profile, tenant, invoices, payments] = await Promise.all([
      api<BillingSubscription>("/billing/subscription"),
      api<BillingUsage[]>("/billing/usage"),
      api<BillingPlans>("/billing/plans"),
      api<BillingProfile | null>("/billing/profile"),
      api<Tenant>("/tenants/current"),
      api<InvoiceSummary[]>("/billing/invoices"),
      api<BillingTransaction[]>("/billing/transactions"),
    ]);
    setData({ sub, usage, catalogue, profile, tenant, invoices, payments });
  }, []);

  useEffect(() => {
    void Promise.resolve()
      .then(load)
      .catch((e) => setError(errorMessage(e)));
  }, [load]);

  // Back from Stripe Checkout.
  useEffect(() => {
    const result = params.get("checkout");
    const sessionId = params.get("session_id");
    if (!result || confirmed.current) return;
    confirmed.current = true;
    if (result === "success" && sessionId) {
      api<BillingSubscription>("/billing/checkout/stripe/confirm", { method: "POST", body: { session_id: sessionId } })
        .then((sub) => {
          setNotice(sub.status === "active" ? `You're on ${sub.plan_name}. Thank you!` : "Payment received. Your plan will update in a moment.");
          return load();
        })
        .catch((e) => setProblem(toProblem(e)));
    } else if (result === "canceled") {
      void Promise.resolve().then(() => setNotice("Checkout was closed. Nothing was charged."));
    }
    router.replace("/billing");
  }, [params, router, load]);

  async function checkout(priceId: string, seats: number) {
    if (!data?.profile) {
      throw new Error("Add your billing details below first.");
    }
    const start = await api<CheckoutStart>("/billing/checkout", { method: "POST", body: { price_id: priceId, extra_seats: seats } });
    if (start.gateway === "stripe" && start.stripe_url) {
      window.location.assign(start.stripe_url);
      return;
    }
    const result = await payWithRazorpay(start.razorpay ?? {});
    if (!result) {
      setNotice("Checkout was closed. Nothing was charged.");
      return;
    }
    const sub = await api<BillingSubscription>("/billing/checkout/razorpay/verify", { method: "POST", body: result });
    setNotice(`You're on ${sub.plan_name}. Thank you! Your invoice will appear here shortly.`);
    await load();
  }

  if (error) return <Alert tone="error">{error}</Alert>;
  if (!data) return <PageLoader />;

  const update = (sub: BillingSubscription) => {
    setData((d) => (d ? { ...d, sub } : d));
    load().catch(() => undefined);
  };

  return (
    <>
      <header>
        <h1 className="text-2xl font-semibold">Billing</h1>
        <p className="mt-1 text-sm text-muted">Your plan, usage, invoices and billing details.</p>
      </header>
      {notice && <Alert tone="success">{notice}</Alert>}
      {problem && <ProblemAlert problem={problem} />}
      <PlanStatus sub={data.sub} manage={manage} onChange={update} />
      <UsageMeters usage={data.usage} />
      <PlanPicker
        catalogue={data.catalogue}
        sub={data.sub}
        manage={manage}
        onCheckout={checkout}
        onChanged={(sub, immediate) => {
          setNotice(immediate ? `You're now on ${sub.plan_name}.` : `${sub.pending_plan_name} starts on ${formatDate(sub.current_period_end)}.`);
          update(sub);
        }}
      />
      {manage && data.sub.in_force && data.sub.status !== "trialing" && data.sub.gateway && <Seats sub={data.sub} onChange={update} />}
      <BillingDetails
        profile={data.profile}
        country={data.tenant.country}
        manage={manage}
        onSaved={(profile) => setData((d) => (d ? { ...d, profile } : d))}
      />
      <History invoices={data.invoices} payments={data.payments} />
    </>
  );
}

export default function BillingPage() {
  return (
    <Suspense fallback={<PageLoader />}>
      <Billing />
    </Suspense>
  );
}
