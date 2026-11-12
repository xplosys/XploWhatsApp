"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { SelectField, TextField } from "@/components/ui/Field";
import { api, ApiError, errorMessage } from "@/lib/api/client";
import type { SessionResponse } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { COUNTRIES, timezoneFor } from "@/lib/locale";

export default function RegisterPage() {
  const { acceptSession } = useAuth();
  const router = useRouter();
  const [form, setForm] = useState({ full_name: "", business_name: "", email: "", password: "", country: "IN" });
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setFieldErrors({});
    setBusy(true);
    try {
      const session = await api<SessionResponse>("/auth/register", {
        method: "POST",
        auth: false,
        body: { ...form, timezone: timezoneFor(form.country) },
      });
      await acceptSession(session);
      router.replace("/dashboard");
    } catch (err) {
      if (err instanceof ApiError && err.code === "weak_password") {
        setFieldErrors({ password: err.message });
      } else if (err instanceof ApiError && err.code === "email_taken") {
        setFieldErrors({ email: err.message });
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Create your account</h1>
        <p className="mt-1 text-sm text-muted">Start a 14-day free trial. No card needed.</p>
      </div>
      {error && <Alert tone="error">{error}</Alert>}
      <TextField label="Your name" autoComplete="name" value={form.full_name} onChange={set("full_name")} required />
      <TextField label="Business name" autoComplete="organization" value={form.business_name} onChange={set("business_name")} required />
      <SelectField label="Country" value={form.country} onChange={set("country")} options={COUNTRIES} />
      <TextField label="Work email" type="email" autoComplete="email" value={form.email} onChange={set("email")} error={fieldErrors.email} required />
      <TextField
        label="Password"
        type="password"
        autoComplete="new-password"
        value={form.password}
        onChange={set("password")}
        hint="At least 10 characters."
        error={fieldErrors.password}
        required
      />
      <Button type="submit" loading={busy} className="w-full">
        Create account
      </Button>
      <p className="text-center text-sm text-muted">
        Already have an account?{" "}
        <Link href="/login" className="text-brand hover:underline">
          Sign in
        </Link>
      </p>
    </form>
  );
}
