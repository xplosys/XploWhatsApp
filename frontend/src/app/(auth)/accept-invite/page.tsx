"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { WithToken } from "@/components/TokenPage";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type { InvitationPreview, SessionResponse } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";

function Accept({ token }: { token: string | null }) {
  const { status, me, acceptSession } = useAuth();
  const router = useRouter();
  const [invite, setInvite] = useState<InvitationPreview | null>(null);
  const [error, setError] = useState<string | null>(token ? null : "This link is missing its token.");
  const [form, setForm] = useState({ full_name: "", password: "" });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) return;
    api<InvitationPreview>(`/auth/invitations/${encodeURIComponent(token)}`, { auth: false })
      .then(setInvite)
      .catch((err) => setError(errorMessage(err)));
  }, [token]);

  async function join(e: React.FormEvent) {
    e.preventDefault();
    if (!invite) return;
    setBusy(true);
    setError(null);
    try {
      const session =
        status === "authenticated"
          ? await api<SessionResponse>("/auth/invitations/accept", { method: "POST", body: { token } })
          : await api<SessionResponse>("/auth/register", {
              method: "POST",
              auth: false,
              body: { email: invite.email, invitation_token: token, ...form },
            });
      await acceptSession(session);
      router.replace("/dashboard");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (error && !invite) {
    return (
      <div className="space-y-5">
        <h1 className="text-xl font-semibold">Invitation</h1>
        <Alert tone="error">{error}</Alert>
        <Link href="/login" className="block text-sm text-brand hover:underline">
          Go to sign in
        </Link>
      </div>
    );
  }
  if (!invite || status === "loading") return <PageLoader />;

  const signedInAsOther = status === "authenticated" && me?.user.email.toLowerCase() !== invite.email.toLowerCase();

  return (
    <form onSubmit={join} className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Join {invite.tenant_name}</h1>
        <p className="mt-1 text-sm text-muted">
          You&apos;ve been invited as <strong className="capitalize">{invite.role}</strong> ({invite.email}).
        </p>
      </div>
      {error && <Alert tone="error">{error}</Alert>}
      {signedInAsOther ? (
        <Alert tone="warning">
          You&apos;re signed in as {me?.user.email}. Sign out and open this link again to accept as {invite.email}.
        </Alert>
      ) : status === "authenticated" ? (
        <Button type="submit" loading={busy} className="w-full">
          Accept invitation
        </Button>
      ) : (
        <>
          <TextField label="Your name" autoComplete="name" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} required />
          <TextField label="Choose a password" type="password" autoComplete="new-password" hint="At least 10 characters." value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
          <Button type="submit" loading={busy} className="w-full">
            Create account and join
          </Button>
          <p className="text-center text-sm text-muted">
            Already have an account?{" "}
            <Link href={`/login`} className="text-brand hover:underline">
              Sign in
            </Link>{" "}
            and open this link again.
          </p>
        </>
      )}
    </form>
  );
}

export default function AcceptInvitePage() {
  return <WithToken render={(token) => <Accept token={token} />} />;
}
