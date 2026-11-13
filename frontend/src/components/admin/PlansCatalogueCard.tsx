"use client";

import { useCallback, useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField, TextAreaField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type { AdminPlan } from "@/lib/api/types";
import { errorDetails, formatMoney } from "@/lib/billing";
import { formatDate } from "@/lib/locale";

const LIMITS: { key: string; label: string }[] = [
  { key: "max_users", label: "Team members" },
  { key: "max_whatsapp_numbers", label: "WhatsApp numbers" },
  { key: "max_knowledge_documents", label: "Knowledge documents" },
  { key: "max_storage_mb", label: "Storage (MB)" },
  { key: "monthly_ai_messages", label: "AI replies a month" },
  { key: "platform_ai_keys", label: "XploSys AI keys (1 = yes)" },
];

type PlanForm = {
  code: string;
  name: string;
  description: string;
  features: string;
  sort_order: string;
  limits: Record<string, string>;
};

const emptyForm = (plan?: AdminPlan): PlanForm => ({
  code: plan?.code ?? "",
  name: plan?.name ?? "",
  description: plan?.description ?? "",
  features: (plan?.features ?? []).join("\n"),
  sort_order: String(plan?.sort_order ?? 0),
  limits: Object.fromEntries(LIMITS.map((l) => [l.key, plan?.limits[l.key] !== undefined ? String(plan.limits[l.key]) : ""])),
});

function PlanEditor({
  plan,
  busy,
  onSave,
  onCancel,
}: {
  plan?: AdminPlan;
  busy: boolean;
  onSave: (body: Record<string, unknown>) => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState<PlanForm>(emptyForm(plan));
  function submit(e: React.FormEvent) {
    e.preventDefault();
    const limits = Object.fromEntries(
      Object.entries(form.limits)
        .filter(([, v]) => v.trim() !== "")
        .map(([k, v]) => [k, Number(v)]),
    );
    onSave({
      ...(plan ? {} : { code: form.code.trim() }),
      name: form.name.trim(),
      description: form.description.trim() || null,
      features: form.features.split("\n").map((f) => f.trim()).filter(Boolean),
      sort_order: Number(form.sort_order) || 0,
      limits,
    });
  }
  return (
    <form onSubmit={submit} className="space-y-4 rounded-control border border-line p-4">
      <div className="grid gap-4 md:grid-cols-3">
        {!plan && (
          <TextField label="Code" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} required pattern="[a-z0-9][a-z0-9_\-]{1,63}" hint="Lowercase, e.g. growth" />
        )}
        <TextField label="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        <TextField label="Order on the page" type="number" value={form.sort_order} onChange={(e) => setForm({ ...form, sort_order: e.target.value })} />
      </div>
      <TextField label="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} maxLength={500} />
      <div className="grid gap-4 md:grid-cols-3">
        {LIMITS.map((l) => (
          <TextField
            key={l.key}
            label={l.label}
            type="number"
            min={0}
            value={form.limits[l.key]}
            required={!plan}
            onChange={(e) => setForm({ ...form, limits: { ...form.limits, [l.key]: e.target.value } })}
          />
        ))}
      </div>
      <TextAreaField label="Features (one per line)" value={form.features} onChange={(e) => setForm({ ...form, features: e.target.value })} hint="Shown on the plan card, in this order." />
      <div className="flex gap-2">
        <Button type="submit" loading={busy}>
          {plan ? "Save plan" : "Create plan"}
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function PriceForm({ busy, onSave }: { busy: boolean; onSave: (body: Record<string, unknown>) => void }) {
  const [form, setForm] = useState({ currency: "INR", interval: "month", amount: "", seat_amount: "" });
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSave({ ...form, seat_amount: form.seat_amount || null });
      }}
      className="grid items-end gap-3 rounded-control border border-line p-4 md:grid-cols-5"
    >
      <SelectField
        label="Currency"
        value={form.currency}
        onChange={(e) => setForm({ ...form, currency: e.target.value })}
        options={["INR", "USD", "GBP", "EUR"].map((c) => ({ value: c, label: c }))}
      />
      <SelectField
        label="Billed"
        value={form.interval}
        onChange={(e) => setForm({ ...form, interval: e.target.value })}
        options={[
          { value: "month", label: "Monthly" },
          { value: "year", label: "Yearly" },
        ]}
      />
      <TextField label="Price before tax" type="number" min="0.01" step="0.01" required value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} />
      <TextField label="Per extra seat (optional)" type="number" min="0.01" step="0.01" value={form.seat_amount} onChange={(e) => setForm({ ...form, seat_amount: e.target.value })} />
      <Button type="submit" loading={busy}>
        Add price
      </Button>
      <p className="text-xs text-muted md:col-span-5">
        INR prices go to Razorpay with GST added when a seller GSTIN is configured; other currencies go to Stripe. A new price
        replaces the active one for the same currency and interval; current subscribers keep theirs.
      </p>
    </form>
  );
}

/** Super Admin: subscription plans, limits and prices. Nothing about pricing is in code. */
export function PlansCatalogueCard() {
  const [plans, setPlans] = useState<AdminPlan[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [details, setDetails] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [pricing, setPricing] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api<AdminPlan[]>("/admin/billing/plans").then(
        (list) => setPlans(list),
        (err) => setError(errorMessage(err)),
      ),
    [],
  );

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  async function run(key: string, action: () => Promise<unknown>, done: string, after?: () => void) {
    setBusy(key);
    setError(null);
    setDetails([]);
    setNotice(null);
    try {
      await action();
      setNotice(done);
      after?.();
      await load();
    } catch (err) {
      setError(errorMessage(err));
      setDetails(errorDetails(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card
      title="Plans and prices"
      description="What businesses can subscribe to. Limit changes apply to every subscriber straight away."
      actions={
        editing !== "new" && (
          <Button variant="secondary" onClick={() => setEditing("new")}>
            New plan
          </Button>
        )
      }
    >
      <div className="space-y-4">
        {error && (
          <Alert tone="error">
            {error}
            {details.length > 0 && <span className="block">{details.join(" ")}</span>}
          </Alert>
        )}
        {notice && <Alert tone="success">{notice}</Alert>}
        {editing === "new" && (
          <PlanEditor
            busy={busy === "new"}
            onCancel={() => setEditing(null)}
            onSave={(body) => run("new", () => api("/admin/billing/plans", { method: "POST", body }), "Plan created.", () => setEditing(null))}
          />
        )}
        {!plans ? (
          !error && <PageLoader />
        ) : plans.length === 0 ? (
          <EmptyState title="No plans yet" description="Run the seed command to create the trial plan." />
        ) : (
          plans.map((plan) => (
            <div key={plan.id} className="rounded-control border border-line p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="font-semibold">
                    {plan.name} <span className="font-normal text-muted">({plan.code})</span>
                  </p>
                  <p className="mt-1 flex flex-wrap gap-1.5">
                    {plan.code === "trial" ? (
                      <Badge tone="brand">{plan.trial_days}-day trial for new businesses</Badge>
                    ) : (
                      <>
                        {!plan.is_active && <Badge tone="danger">Off</Badge>}
                        {plan.is_active && !plan.is_public && <Badge>Hidden</Badge>}
                        {plan.is_active && plan.is_public && <Badge tone="success">On sale</Badge>}
                      </>
                    )}
                    <Badge>
                      {plan.subscribers} subscriber{plan.subscribers === 1 ? "" : "s"}
                    </Badge>
                  </p>
                  <p className="mt-2 text-xs text-muted">
                    {LIMITS.filter((l) => plan.limits[l.key] !== undefined)
                      .map((l) => `${l.label}: ${plan.limits[l.key]}`)
                      .join(" · ")}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button variant="ghost" className="px-3 py-1.5" onClick={() => setEditing(editing === plan.id ? null : plan.id)}>
                    Edit
                  </Button>
                  {plan.code !== "trial" && (
                    <>
                      <Button variant="ghost" className="px-3 py-1.5" onClick={() => setPricing(pricing === plan.id ? null : plan.id)}>
                        Add price
                      </Button>
                      <Button
                        variant="ghost"
                        className="px-3 py-1.5"
                        loading={busy === `pub-${plan.id}`}
                        onClick={() =>
                          run(
                            `pub-${plan.id}`,
                            () => api(`/admin/billing/plans/${plan.id}`, { method: "PATCH", body: { is_public: !plan.is_public } }),
                            plan.is_public ? "Plan hidden from the plan picker." : "Plan is on sale.",
                          )
                        }
                      >
                        {plan.is_public ? "Hide" : "Show"}
                      </Button>
                      <Button
                        variant={plan.is_active ? "danger" : "secondary"}
                        className="px-3 py-1.5"
                        loading={busy === `act-${plan.id}`}
                        onClick={() =>
                          run(
                            `act-${plan.id}`,
                            () => api(`/admin/billing/plans/${plan.id}`, { method: "PATCH", body: { is_active: !plan.is_active } }),
                            plan.is_active ? "Plan turned off for new sales." : "Plan turned on.",
                          )
                        }
                      >
                        {plan.is_active ? "Turn off" : "Turn on"}
                      </Button>
                    </>
                  )}
                </div>
              </div>
              {editing === plan.id && (
                <div className="mt-4">
                  <PlanEditor
                    plan={plan}
                    busy={busy === plan.id}
                    onCancel={() => setEditing(null)}
                    onSave={(body) =>
                      run(plan.id, () => api(`/admin/billing/plans/${plan.id}`, { method: "PATCH", body }), "Plan saved.", () => setEditing(null))
                    }
                  />
                </div>
              )}
              {pricing === plan.id && (
                <div className="mt-4">
                  <PriceForm
                    busy={busy === `price-${plan.id}`}
                    onSave={(body) =>
                      run(
                        `price-${plan.id}`,
                        () => api(`/admin/billing/plans/${plan.id}/prices`, { method: "POST", body }),
                        "Price added and created on the gateway.",
                        () => setPricing(null),
                      )
                    }
                  />
                </div>
              )}
              {plan.prices.length > 0 && (
                <div className="mt-4 overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead className="text-xs uppercase text-muted">
                      <tr>
                        <th className="py-1.5 pr-4 font-medium">Price</th>
                        <th className="py-1.5 pr-4 font-medium">Charged</th>
                        <th className="py-1.5 pr-4 font-medium">Extra seat</th>
                        <th className="py-1.5 pr-4 font-medium">Gateway</th>
                        <th className="py-1.5 pr-4 font-medium">Since</th>
                        <th className="py-1.5" />
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line">
                      {plan.prices.map((p) => (
                        <tr key={p.id} className={p.is_active ? "" : "text-muted"}>
                          <td className="py-2 pr-4 tabular-nums">
                            {formatMoney(p.amount, p.currency)} / {p.interval}
                          </td>
                          <td className="py-2 pr-4 tabular-nums">
                            {formatMoney(p.amount_with_tax, p.currency)}
                            {Number(p.tax_rate) > 0 && <span className="text-xs text-muted"> incl. {Number(p.tax_rate)}% GST</span>}
                          </td>
                          <td className="py-2 pr-4 tabular-nums">{p.seat_amount ? formatMoney(p.seat_amount, p.currency) : "—"}</td>
                          <td className="py-2 pr-4">
                            <span className="capitalize">{p.gateway}</span>{" "}
                            <span className="text-xs text-muted">{p.gateway_plan_id}</span>
                          </td>
                          <td className="py-2 pr-4">{formatDate(p.created_at)}</td>
                          <td className="py-2 text-right">
                            {p.is_active ? (
                              <Button
                                variant="ghost"
                                className="px-3 py-1"
                                loading={busy === p.id}
                                onClick={() =>
                                  run(p.id, () => api(`/admin/billing/plans/${plan.id}/prices/${p.id}`, { method: "DELETE" }), "Price retired. Current subscribers keep it.")
                                }
                              >
                                Retire
                              </Button>
                            ) : (
                              <Badge>Retired</Badge>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </Card>
  );
}
