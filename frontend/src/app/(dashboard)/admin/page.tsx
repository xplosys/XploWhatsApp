"use client";

import { useEffect, useState } from "react";

import { AICatalogueCard } from "@/components/admin/AICatalogueCard";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, ApiError, errorMessage } from "@/lib/api/client";
import type { AdminTenant, Page } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatDate } from "@/lib/locale";

function fetchTenants(query: string) {
  return api<Page<AdminTenant>>(`/admin/tenants?limit=50${query ? `&q=${encodeURIComponent(query)}` : ""}`);
}

export default function AdminPage() {
  const { me } = useAuth();
  const [page, setPage] = useState<Page<AdminTenant> | null>(null);
  const [q, setQ] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const fail = (err: unknown) => {
    setPage(null);
    setError(
      err instanceof ApiError && err.code === "mfa_required"
        ? "Sign out and sign in again with your two-factor code to use the admin console."
        : errorMessage(err),
    );
  };
  const show = (result: Page<AdminTenant>) => {
    setError(null);
    setPage(result);
  };

  useEffect(() => {
    if (me?.user.is_super_admin) fetchTenants("").then(show).catch(fail);
  }, [me]);

  const load = (query: string) => fetchTenants(query).then(show).catch(fail);

  if (!me?.user.is_super_admin) return <Alert tone="error">You don&apos;t have access to this page.</Alert>;

  async function toggle(t: AdminTenant) {
    setBusy(t.id);
    try {
      await api(`/admin/tenants/${t.id}/${t.status === "active" ? "suspend" : "reactivate"}`, { method: "POST" });
      await load(q);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <header>
        <h1 className="text-2xl font-semibold">Super Admin</h1>
        <p className="mt-1 text-sm text-muted">All businesses on the platform. Every action here is audited.</p>
      </header>
      {error && <Alert tone="error">{error}</Alert>}
      <Card
        title="Businesses"
        actions={
          <form
            className="flex items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              load(q);
            }}
          >
            <TextField label="Search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Name or slug" />
            <Button type="submit" variant="secondary">
              Search
            </Button>
          </form>
        }
      >
        {!page && !error ? (
          <PageLoader />
        ) : page && page.items.length === 0 ? (
          <EmptyState title="No businesses found" />
        ) : (
          page && (
            <div className="-mx-6 overflow-x-auto">
              <table className="w-full min-w-[720px] text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-muted">
                    <th className="px-6 py-2 font-medium">Business</th>
                    <th className="px-3 py-2 font-medium">Plan</th>
                    <th className="px-3 py-2 font-medium">Members</th>
                    <th className="px-3 py-2 font-medium">Created</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-6 py-2" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {page.items.map((t) => (
                    <tr key={t.id}>
                      <td className="px-6 py-3">
                        <p className="font-medium">{t.name}</p>
                        <p className="text-xs text-muted">
                          {t.slug} · {t.country}
                        </p>
                      </td>
                      <td className="px-3 py-3">
                        {t.plan_code ?? "—"} {t.subscription_status && <span className="text-muted">({t.subscription_status})</span>}
                      </td>
                      <td className="px-3 py-3">{t.member_count}</td>
                      <td className="px-3 py-3">{formatDate(t.created_at)}</td>
                      <td className="px-3 py-3">
                        <Badge tone={t.status === "active" ? "success" : "danger"}>{t.status}</Badge>
                      </td>
                      <td className="px-6 py-3 text-right">
                        <Button variant={t.status === "active" ? "danger" : "secondary"} className="py-1" loading={busy === t.id} onClick={() => toggle(t)}>
                          {t.status === "active" ? "Suspend" : "Reactivate"}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="px-6 pt-3 text-xs text-muted">
                Showing {page.items.length} of {page.total}
              </p>
            </div>
          )
        )}
      </Card>
      {!error && <AICatalogueCard />}
    </>
  );
}
