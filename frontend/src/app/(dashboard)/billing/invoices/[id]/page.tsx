"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type { Invoice } from "@/lib/api/types";
import { formatMoney } from "@/lib/billing";
import { formatDate } from "@/lib/locale";

function Party({ title, party }: { title: string; party: Record<string, string | null> }) {
  const lines = [
    party.address,
    party.address_line1,
    party.address_line2,
    [party.city, party.postal_code].filter(Boolean).join(" "),
    party.state && `${party.state}${party.state_code ? ` (${party.state_code})` : ""}`,
    party.country && party.country !== "IN" ? party.country : null,
  ].filter(Boolean);
  return (
    <div>
      <p className="text-xs font-semibold uppercase tracking-wide text-muted">{title}</p>
      <p className="mt-1 font-semibold">{party.legal_name}</p>
      {lines.map((l) => (
        <p key={l} className="text-ink-2">
          {l}
        </p>
      ))}
      {party.gstin && <p className="mt-1">GSTIN: {party.gstin}</p>}
      {party.tax_id && <p className="mt-1">Tax ID: {party.tax_id}</p>}
      {party.email && <p className="text-ink-2">{party.email}</p>}
    </div>
  );
}

export default function InvoicePage() {
  const { id } = useParams<{ id: string }>();
  const [invoice, setInvoice] = useState<Invoice | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Invoice>(`/billing/invoices/${id}`)
      .then(setInvoice)
      .catch((e) => setError(errorMessage(e)));
  }, [id]);

  if (error) return <Alert tone="error">{error}</Alert>;
  if (!invoice) return <PageLoader />;
  const money = (v: string) => formatMoney(v, invoice.currency);
  const gst = invoice.taxes.length > 0 || invoice.seller.gstin;

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3 print:hidden">
        <Link href="/billing" className="text-sm font-semibold text-brand hover:underline">
          Back to billing
        </Link>
        <Button variant="secondary" onClick={() => window.print()}>
          Print or save as PDF
        </Button>
      </div>
      <article className="rounded-card border border-line bg-surface p-8 text-sm shadow-card print:border-0 print:p-0 print:shadow-none">
        <header className="flex flex-wrap items-start justify-between gap-4 border-b border-line pb-6">
          <div>
            <h1 className="text-2xl font-semibold">{gst ? "Tax invoice" : "Invoice"}</h1>
            <p className="mt-1 text-ink-2">Invoice number {invoice.number}</p>
          </div>
          <div className="text-right text-ink-2">
            <p>Date: {formatDate(invoice.issued_at)}</p>
            {invoice.period_start && (
              <p>
                Period: {formatDate(invoice.period_start)} to {formatDate(invoice.period_end)}
              </p>
            )}
            {invoice.place_of_supply && <p>Place of supply: {invoice.place_of_supply}</p>}
          </div>
        </header>
        <div className="grid gap-6 border-b border-line py-6 md:grid-cols-2">
          <Party title="From" party={invoice.seller} />
          <Party title="Billed to" party={invoice.buyer} />
        </div>
        <table className="mt-6 w-full text-left">
          <thead className="text-xs uppercase text-muted">
            <tr>
              <th className="py-2 pr-4 font-medium">Description</th>
              <th className="py-2 pr-4 font-medium">SAC</th>
              <th className="py-2 pr-4 text-right font-medium">Qty</th>
              <th className="py-2 text-right font-medium">Taxable value</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {invoice.line_items.map((l, i) => (
              <tr key={i}>
                <td className="py-2.5 pr-4">{l.description}</td>
                <td className="py-2.5 pr-4 text-muted">{l.sac ?? "—"}</td>
                <td className="py-2.5 pr-4 text-right tabular-nums">{l.quantity}</td>
                <td className="py-2.5 text-right tabular-nums">{money(l.amount)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <dl className="ml-auto mt-6 w-full max-w-xs space-y-1.5 tabular-nums">
          <div className="flex justify-between">
            <dt className="text-ink-2">Subtotal</dt>
            <dd>{money(invoice.subtotal)}</dd>
          </div>
          {invoice.taxes.map((t) => (
            <div key={t.name} className="flex justify-between">
              <dt className="text-ink-2">
                {t.name} at {t.rate}%
              </dt>
              <dd>{money(t.amount)}</dd>
            </div>
          ))}
          <div className="flex justify-between border-t border-line pt-2 text-base font-semibold">
            <dt>Total</dt>
            <dd>{money(invoice.total)}</dd>
          </div>
        </dl>
        <footer className="mt-8 space-y-1 border-t border-line pt-4 text-xs text-muted">
          {invoice.tax_note && <p>{invoice.tax_note}.</p>}
          <p>
            Paid through {invoice.gateway === "razorpay" ? "Razorpay" : "Stripe"}, reference {invoice.gateway_payment_id}.
          </p>
          <p>This is a computer-generated invoice and needs no signature.</p>
        </footer>
      </article>
    </>
  );
}
