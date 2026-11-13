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
import { useAuth } from "@/lib/auth/AuthProvider";

const NAV = [
  { href: "/dashboard", label: "Overview" },
  { href: "/conversations", label: "Conversations", permission: "conversations:read" },
  { href: "/whatsapp", label: "WhatsApp", permission: "whatsapp:read" },
  { href: "/ai", label: "AI assistant", permission: "ai:configure" },
  { href: "/knowledge", label: "Knowledge base", permission: "knowledge:read" },
  { href: "/team", label: "Team", permission: "members:read" },
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
      <aside className="border-b border-line bg-surface md:min-h-screen md:w-64 md:border-b-0 md:border-r">
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
      <main className="flex-1 px-4 py-8 md:px-10">
        <div className="mx-auto max-w-5xl space-y-6">
          {!me.user.email_verified && <VerifyBanner />}
          {!me.active_tenant_id && !me.user.is_super_admin && (
            <Alert tone="info">You are not a member of any business yet. Ask an owner to invite you.</Alert>
          )}
          {children}
        </div>
      </main>
    </div>
  );
}
