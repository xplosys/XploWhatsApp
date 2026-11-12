"use client";

import { useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { SelectField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type { Tenant } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { COUNTRIES, LANGUAGES, TIMEZONES } from "@/lib/locale";

const INDUSTRIES = [
  { value: "", label: "Select an industry" },
  { value: "healthcare", label: "Hospitals & healthcare" },
  { value: "hospitality", label: "Hotels & restaurants" },
  { value: "ecommerce", label: "E-commerce" },
  { value: "retail", label: "Retail & fashion" },
  { value: "education", label: "Education" },
  { value: "real_estate", label: "Real estate" },
  { value: "professional_services", label: "Consultants & professional services" },
  { value: "other", label: "Other" },
];

export default function SettingsPage() {
  const { can, reload } = useAuth();
  const editable = can("tenant:update");
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [form, setForm] = useState({ name: "", country: "IN", timezone: "Asia/Kolkata", default_language: "en", industry: "" });
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Tenant>("/tenants/current")
      .then((t) => {
        setTenant(t);
        setForm({ name: t.name, country: t.country, timezone: t.timezone, default_language: t.default_language, industry: t.industry ?? "" });
      })
      .catch((e) => setError(errorMessage(e)));
  }, []);

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    setSaved(false);
    setForm((f) => ({ ...f, [k]: e.target.value }));
  };

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body = { ...form, industry: form.industry || undefined };
      setTenant(await api<Tenant>("/tenants/current", { method: "PUT", body }));
      setSaved(true);
      await reload();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (!tenant && !error) return <PageLoader />;

  return (
    <>
      <header>
        <h1 className="text-2xl font-semibold">Business settings</h1>
        <p className="mt-1 text-sm text-muted">Used by your assistant and in messages to customers.</p>
      </header>
      {error && <Alert tone="error">{error}</Alert>}
      {saved && <Alert tone="success">Settings saved.</Alert>}
      {tenant && (
        <Card>
          <form onSubmit={onSubmit} className="grid gap-5 md:grid-cols-2">
            <div className="md:col-span-2">
              <TextField label="Business name" value={form.name} onChange={set("name")} disabled={!editable} required />
            </div>
            <SelectField label="Industry" value={form.industry} onChange={set("industry")} options={INDUSTRIES} disabled={!editable} />
            <SelectField label="Country" value={form.country} onChange={set("country")} options={COUNTRIES} disabled={!editable} />
            <SelectField
              label="Time zone"
              value={form.timezone}
              onChange={set("timezone")}
              options={TIMEZONES.map((t) => ({ value: t, label: t }))}
              disabled={!editable}
            />
            <SelectField label="Default language" value={form.default_language} onChange={set("default_language")} options={LANGUAGES} disabled={!editable} />
            {editable ? (
              <div className="md:col-span-2">
                <Button type="submit" loading={busy}>
                  Save changes
                </Button>
              </div>
            ) : (
              <p className="text-sm text-muted md:col-span-2">Only owners and admins can change these settings.</p>
            )}
          </form>
        </Card>
      )}
    </>
  );
}
