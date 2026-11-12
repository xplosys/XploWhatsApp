"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { Alert } from "@/components/ui/Alert";
import { api, errorMessage } from "@/lib/api/client";
import type { Subscription } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatDate } from "@/lib/locale";

function Step({ done, title, href, action }: { done: boolean; title: string; href?: string; action?: string }) {
  return (
    <li className="flex items-center justify-between gap-3 py-3">
      <span className="flex items-center gap-3 text-sm">
        <span
          aria-hidden
          className={`flex h-5 w-5 items-center justify-center rounded-full text-xs ${done ? "bg-brand-gradient text-white" : "border-[1.5px] border-line"}`}
        >
          {done ? "✓" : ""}
        </span>
        <span className={done ? "text-muted line-through" : ""}>{title}</span>
      </span>
      {!done && href && (
        <Link href={href} className="text-sm font-medium text-brand hover:underline">
          {action}
        </Link>
      )}
    </li>
  );
}

export default function OverviewPage() {
  const { me, can } = useAuth();
  const [sub, setSub] = useState<Subscription | null>(null);
  const [error, setError] = useState<string | null>(null);
  const canBilling = can("billing:read");

  useEffect(() => {
    if (!canBilling) return;
    api<Subscription>("/billing/subscription").then(setSub).catch((e) => setError(errorMessage(e)));
  }, [canBilling, me?.active_tenant_id]);

  if (!me) return null;
  const tenant = me.memberships.find((m) => m.tenant_id === me.active_tenant_id);

  return (
    <>
      <header>
        <h1 className="text-2xl font-semibold">Welcome, {me.user.full_name.split(" ")[0]}</h1>
        {tenant && (
          <p className="mt-1 text-sm text-muted">
            {tenant.tenant_name} · <span className="capitalize">{me.role}</span>
          </p>
        )}
      </header>
      {error && <Alert tone="error">{error}</Alert>}
      <div className="grid gap-6 md:grid-cols-2">
        <Card title="Get set up" description="Finish these to secure your account and bring in your team.">
          <ul className="divide-y divide-line">
            <Step done={me.user.email_verified} title="Verify your email address (use the banner above to resend)" />
            <Step done={me.user.mfa_enabled} title="Turn on two-factor authentication" href="/account" action="Set up" />
            {can("members:invite") && (
              <Step done={(sub?.seats_used ?? 1) > 1} title="Invite a teammate" href="/team" action="Invite" />
            )}
          </ul>
        </Card>
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
    </>
  );
}
