"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Logo } from "@/components/Logo";
import { ThemeToggle } from "@/components/ThemeToggle";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type { BillingSubscription } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatDate } from "@/lib/locale";

const NAV = [
  { href: "/dashboard", label: "Overview" },
  { href: "/conversations", label: "Conversations", permission: "conversations:read" },
  { href: "/contacts", label: "Contacts", permission: "conversations:read" },
  { href: "/leads", label: "Leads", permission: "leads:manage" },
  { href: "/analytics", label: "Analytics", permission: "analytics:read" },
  { href: "/whatsapp", label: "WhatsApp", permission: "whatsapp:read" },
  { href: "/ai", label: "AI assistant", permission: "ai:configure" },
  { href: "/knowledge", label: "Knowledge base", permission: "knowledge:read" },
  { href: "/team", label: "Team", permission: "members:read" },
  { href: "/billing", label: "Billing", permission: "billing:read" },
  { href: "/settings", label: "Business settings", permission: "tenant:read" },
  { href: "/account", label: "Account & security" },
];

function VerifyBanner() {
  const [state, setState] = useState<"idle" | "sending" | "sent" | string>("idle");
  async function resend() {
    setState("sending");
    try {
      await api("/auth/resend-verification", { method: "POST" });
      setState("sent");
    } catch (err) {
      setState(errorMessage(err));
    }
  }
  return (
    <Alert tone="warning">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span>
          {state === "sent"
            ? "Verification email sent. Check your inbox."
            : state.length > 7 && state !== "sending"
              ? state
              : "Please verify your email address. Some features need a verified email."}
        </span>
        {state !== "sent" && (
          <Button variant="secondary" onClick={resend} loading={state === "sending"} className="py-1">
            Resend email
          </Button>
        )}
      </div>
    </Alert>
  );
}

/** Tells owners and admins when the plan is about to stop, or has. */
function BillingBanner({ tenantId, pathname }: { tenantId: string; pathname: string }) {
  const [sub, setSub] = useState<BillingSubscription | null>(null);
  const [trialDays, setTrialDays] = useState<number | null>(null);
  useEffect(() => {
    api<BillingSubscription>("/billing/subscription")
      .then((s) => {
        setSub(s);
        setTrialDays(s.trial_ends_at ? Math.ceil((new Date(s.trial_ends_at).getTime() - Date.now()) / 86_400_000) : null);
      })
      .catch(() => setSub(null));
  }, [tenantId]);
  if (!sub || pathname.startsWith("/billing")) return null;
  let text: string | null = null;
  let tone: "warning" | "error" = "warning";
  if (!sub.in_force) {
    text = "Your plan has ended, so the AI assistant has stopped replying. Choose a plan to carry on.";
    tone = "error";
  } else if (sub.status === "past_due") {
    text = `Your latest payment didn't go through. Update it by ${formatDate(sub.grace_ends_at)} to keep your plan.`;
  } else if (sub.status === "trialing" && trialDays !== null) {
    const days = Math.max(1, trialDays);
    if (days <= 3) text = `Your free trial ends in ${days} day${days === 1 ? "" : "s"}. Choose a plan to keep the AI assistant answering.`;
  }
  if (!text) return null;
  return (
    <Alert tone={tone}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span>{text}</span>
        <Link href="/billing" className="font-semibold underline">
          Go to billing
        </Link>
      </div>
    </Alert>
  );
}

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const { status, me, can, logout, switchTenant } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    if (status === "anonymous") router.replace("/login");
  }, [status, router]);

  if (status !== "authenticated" || !me) return <PageLoader />;

  const active = me.memberships.find((m) => m.tenant_id === me.active_tenant_id);
  const nav = [
    ...NAV.filter((n) => !n.permission || can(n.permission)),
    ...(me.user.is_super_admin ? [{ href: "/admin", label: "Super Admin" }] : []),
  ];

  return (
    <div className="min-h-screen md:flex">
      <aside className="print:hidden border-b border-line bg-surface md:min-h-screen md:w-64 md:border-b-0 md:border-r">
        <div className="flex items-center justify-between px-5 py-4">
          <Logo />
          <span className="flex items-center gap-2">
            <ThemeToggle />
            <Button variant="secondary" className="px-4 py-2 md:hidden" onClick={() => setMenuOpen((o) => !o)} aria-expanded={menuOpen}>
              Menu
            </Button>
          </span>
        </div>
        <div className={`${menuOpen ? "block" : "hidden"} px-3 pb-4 md:block`}>
          {me.memberships.length > 1 ? (
            <select
              aria-label="Switch business"
              className="mb-4 w-full rounded-control border-[1.5px] border-line bg-surface px-3 py-2 text-sm text-ink"
              value={me.active_tenant_id ?? ""}
              onChange={(e) => switchTenant(e.target.value)}
            >
              {me.memberships
                .filter((m) => m.status === "active")
                .map((m) => (
                  <option key={m.tenant_id} value={m.tenant_id}>
                    {m.tenant_name}
                  </option>
                ))}
            </select>
          ) : (
            active && <p className="mb-4 px-2 text-sm font-medium text-ink">{active.tenant_name}</p>
          )}
          <nav className="space-y-1">
            {nav.map((n) => (
              <Link
                key={n.href}
                href={n.href}
                onClick={() => setMenuOpen(false)}
                className={`block rounded-control px-3 py-2 text-sm ${
                  pathname === n.href ? "bg-brand-soft font-semibold text-brand-ink" : "font-medium text-ink-2 hover:bg-surface-muted hover:text-brand"
                }`}
              >
                {n.label}
              </Link>
            ))}
          </nav>
          <div className="mt-6 border-t border-line px-2 pt-4">
            <p className="truncate text-sm font-medium">{me.user.full_name}</p>
            <p className="truncate text-xs text-muted">{me.user.email}</p>
            <Button variant="ghost" className="mt-2 -ml-2 px-2" onClick={() => logout().then(() => router.replace("/login"))}>
              Sign out
            </Button>
          </div>
        </div>
      </aside>
      <main className="min-w-0 flex-1 px-4 py-8 md:px-10 print:p-0">
        <div className="mx-auto max-w-5xl space-y-6">
          <div className="space-y-6 print:hidden">
            {!me.user.email_verified && <VerifyBanner />}
            {me.active_tenant_id && can("billing:read") && <BillingBanner tenantId={me.active_tenant_id} pathname={pathname} />}
          </div>
          {!me.active_tenant_id && !me.user.is_super_admin && (
            <Alert tone="info">You are not a member of any business yet. Ask an owner to invite you.</Alert>
          )}
          {children}
        </div>
      </main>
    </div>
  );
}
