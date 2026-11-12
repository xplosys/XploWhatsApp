"use client";

import { useCallback, useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type { Invitation, Member, Page, TenantRole } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatDate } from "@/lib/locale";

const RANK: Record<TenantRole, number> = { agent: 1, supervisor: 2, admin: 3, owner: 4 };
const ROLE_LABEL: Record<TenantRole, string> = {
  owner: "Owner",
  admin: "Admin",
  supervisor: "Supervisor",
  agent: "Support agent",
};

/** Roles the current user may grant (mirrors app/modules/rbac/permissions.py). */
function assignable(actor: TenantRole | null): TenantRole[] {
  if (actor === "owner") return ["admin", "supervisor", "agent"];
  if (actor === "admin") return ["supervisor", "agent"];
  return [];
}

function canManage(actor: TenantRole | null, target: TenantRole): boolean {
  if (actor === "owner") return true;
  if (actor === "admin") return RANK[actor] > RANK[target];
  return false;
}

export default function TeamPage() {
  const { me, can } = useAuth();
  const [members, setMembers] = useState<Member[] | null>(null);
  const [invites, setInvites] = useState<Invitation[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [form, setForm] = useState<{ email: string; role: TenantRole }>({ email: "", role: "agent" });
  const [busy, setBusy] = useState<string | null>(null);
  const canInvite = can("members:invite");
  const canManageMembers = can("members:manage");

  const fetchAll = useCallback(async () => {
    const [m, i] = await Promise.all([
      api<Page<Member>>("/tenants/current/members?limit=100"),
      canInvite ? api<Page<Invitation>>("/tenants/current/invitations?limit=100") : Promise.resolve(null),
    ]);
    return { members: m.items, invites: i?.items ?? [] };
  }, [canInvite]);

  const apply = (data: { members: Member[]; invites: Invitation[] }) => {
    setMembers(data.members);
    setInvites(data.invites);
  };

  useEffect(() => {
    fetchAll()
      .then(apply)
      .catch((err) => setError(errorMessage(err)));
  }, [fetchAll]);

  async function run(key: string, fn: () => Promise<unknown>, success?: string) {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await fn();
      if (success) setNotice(success);
      apply(await fetchAll());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  if (!me) return null;
  const roles = assignable(me.role);

  return (
    <>
      <header>
        <h1 className="text-2xl font-semibold">Team</h1>
        <p className="mt-1 text-sm text-muted">People who can use this business&apos;s inbox and settings.</p>
      </header>
      {error && <Alert tone="error">{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}

      {canInvite && roles.length > 0 && (
        <Card title="Invite a teammate" description="They'll get an email with a link to join. Invitations expire after 7 days.">
          <form
            className="grid gap-4 md:grid-cols-[1fr_200px_auto] md:items-end"
            onSubmit={(e) => {
              e.preventDefault();
              run("invite", () => api("/tenants/current/invitations", { method: "POST", body: form }), `Invitation sent to ${form.email}.`).then(
                () => setForm((f) => ({ ...f, email: "" })),
              );
            }}
          >
            <TextField label="Email" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
            <SelectField
              label="Role"
              value={form.role}
              onChange={(e) => setForm({ ...form, role: e.target.value as TenantRole })}
              options={roles.map((r) => ({ value: r, label: ROLE_LABEL[r] }))}
            />
            <Button type="submit" loading={busy === "invite"}>
              Send invitation
            </Button>
          </form>
        </Card>
      )}

      <Card title="Members">
        {!members ? (
          <PageLoader />
        ) : (
          <div className="-mx-6 overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-muted">
                  <th className="px-6 py-2 font-medium">Name</th>
                  <th className="px-3 py-2 font-medium">Role</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-6 py-2 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {members.map((m) => {
                  const self = m.user_id === me.user.id;
                  const manageable = canManageMembers && !self && canManage(me.role, m.role);
                  return (
                    <tr key={m.id}>
                      <td className="px-6 py-3">
                        <p className="font-medium">
                          {m.full_name} {self && <span className="text-muted">(you)</span>}
                        </p>
                        <p className="text-xs text-muted">{m.email}</p>
                      </td>
                      <td className="px-3 py-3">
                        {manageable && m.role !== "owner" ? (
                          <select
                            aria-label={`Role for ${m.email}`}
                            className="rounded-control border-[1.5px] border-line bg-surface px-2 py-1 text-sm text-ink"
                            value={m.role}
                            disabled={busy === m.id}
                            onChange={(e) =>
                              run(m.id, () => api(`/tenants/current/members/${m.id}`, { method: "PATCH", body: { role: e.target.value } }), "Role updated.")
                            }
                          >
                            {roles.map((r) => (
                              <option key={r} value={r}>
                                {ROLE_LABEL[r]}
                              </option>
                            ))}
                          </select>
                        ) : (
                          ROLE_LABEL[m.role]
                        )}
                      </td>
                      <td className="px-3 py-3">
                        <Badge tone={m.status === "active" ? "success" : "warning"}>{m.status}</Badge>
                      </td>
                      <td className="px-6 py-3 text-right">
                        {manageable && (
                          <span className="inline-flex gap-2">
                            <Button
                              variant="secondary"
                              className="py-1"
                              loading={busy === `s-${m.id}`}
                              onClick={() =>
                                run(`s-${m.id}`, () =>
                                  api(`/tenants/current/members/${m.id}`, {
                                    method: "PATCH",
                                    body: { status: m.status === "active" ? "suspended" : "active" },
                                  }),
                                )
                              }
                            >
                              {m.status === "active" ? "Suspend" : "Reactivate"}
                            </Button>
                            <Button
                              variant="danger"
                              className="py-1"
                              loading={busy === `r-${m.id}`}
                              onClick={() => {
                                if (confirm(`Remove ${m.full_name} from this business?`)) {
                                  run(`r-${m.id}`, () => api(`/tenants/current/members/${m.id}`, { method: "DELETE" }), "Member removed.");
                                }
                              }}
                            >
                              Remove
                            </Button>
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {canInvite && (
        <Card title="Pending invitations">
          {invites.length === 0 ? (
            <EmptyState title="No pending invitations" />
          ) : (
            <ul className="divide-y divide-line">
              {invites.map((i) => (
                <li key={i.id} className="flex flex-wrap items-center justify-between gap-3 py-3 text-sm">
                  <span>
                    <span className="font-medium">{i.email}</span>
                    <span className="text-muted"> · {ROLE_LABEL[i.role]} · expires {formatDate(i.expires_at)}</span>
                  </span>
                  <Button
                    variant="secondary"
                    className="py-1"
                    loading={busy === i.id}
                    onClick={() => run(i.id, () => api(`/tenants/current/invitations/${i.id}`, { method: "DELETE" }), "Invitation revoked.")}
                  >
                    Revoke
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}
    </>
  );
}
