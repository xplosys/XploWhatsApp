"use client";

import { useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { api, errorMessage } from "@/lib/api/client";

interface Erased {
  conversations: number;
  messages: number;
  leads: number;
}

/** A customer's request to erase their data (DPDP Act / GDPR). Owners and admins only. */
export function EraseContact({ contactId, name, onErased }: { contactId: string; name: string; onErased: (r: Erased) => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function erase() {
    setBusy(true);
    setError(null);
    try {
      onErased(await api<Erased>(`/contacts/${contactId}/erase`, { method: "POST" }));
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Card title="Erase this customer's data" description="When a customer asks you to delete what you hold about them.">
      <div className="space-y-3">
        {error && <Alert tone="error">{error}</Alert>}
        {confirming ? (
          <>
            <Alert tone="warning">
              This permanently deletes {name}, their conversations, messages, notes, summaries and leads. It can&apos;t be undone.
              The audit log keeps only a fingerprint of the number, not the number itself. If they message you again, they start
              as a new contact.
            </Alert>
            <div className="flex flex-wrap gap-2">
              <Button variant="danger" onClick={erase} loading={busy}>
                Erase permanently
              </Button>
              <Button variant="secondary" onClick={() => setConfirming(false)} disabled={busy}>
                Cancel
              </Button>
            </div>
          </>
        ) : (
          <Button variant="danger" onClick={() => setConfirming(true)}>
            Erase customer data
          </Button>
        )}
      </div>
    </Card>
  );
}
