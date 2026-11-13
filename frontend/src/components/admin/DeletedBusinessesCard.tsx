"use client";

import { useCallback, useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { api, errorMessage } from "@/lib/api/client";
import type { DeletedTenant } from "@/lib/api/types";
import { formatDate } from "@/lib/locale";

/** Businesses deleted by their owners, restorable until their data is purged. */
export function DeletedBusinessesCard({ onRestored }: { onRestored?: () => void }) {
  const [rows, setRows] = useState<DeletedTenant[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api<DeletedTenant[]>("/admin/tenants-deleted").then(
        (r) => setRows(r),
        (err) => setError(errorMessage(err)),
      ),
    [],
  );
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  async function restore(t: DeletedTenant) {
    setBusy(t.id);
    setError(null);
    setNotice(null);
    try {
      await api(`/admin/tenants/${t.id}/restore`, { method: "POST" });
      setNotice(`${t.name} is restored. Its team can sign in again.`);
      await load();
      onRestored?.();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card title="Deleted businesses" description="Restore one only when its owner asks. After the purge date its data is gone.">
      {error && <Alert tone="error">{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}
      {rows && rows.length === 0 && <p className="text-sm text-muted">No businesses are waiting to be purged.</p>}
      {rows && rows.length > 0 && (
        <ul className="divide-y divide-line">
          {rows.map((t) => (
            <li key={t.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
              <div>
                <p className="text-sm font-medium">{t.name}</p>
                <p className="text-xs text-muted">
                  Deleted {formatDate(t.deleted_at)} · data removed on {formatDate(t.purge_after)}
                </p>
              </div>
              <Button variant="secondary" loading={busy === t.id} onClick={() => restore(t)}>
                Restore
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
