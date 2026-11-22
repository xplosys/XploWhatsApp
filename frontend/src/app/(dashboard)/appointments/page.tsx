"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField, TextAreaField, TextField } from "@/components/ui/Field";
import { PageLoader } from "@/components/ui/Spinner";
import { api, errorMessage } from "@/lib/api/client";
import type {
  Appointment,
  AppointmentList,
  AppointmentResult,
  AppointmentStatus,
  BookableResource,
  BookingSettings,
  MessageTemplate,
  ResourceAvailability,
  TimeOff,
  WeeklyHours,
} from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatDateTime } from "@/lib/locale";

type Tab = "calendar" | "resources" | "settings";

const WEEKDAYS: { key: keyof WeeklyHours; label: string }[] = [
  { key: "mon", label: "Monday" },
  { key: "tue", label: "Tuesday" },
  { key: "wed", label: "Wednesday" },
  { key: "thu", label: "Thursday" },
  { key: "fri", label: "Friday" },
  { key: "sat", label: "Saturday" },
  { key: "sun", label: "Sunday" },
];

const STATUS: Record<AppointmentStatus, { label: string; tone: "brand" | "success" | "warning" | "neutral" }> = {
  confirmed: { label: "Confirmed", tone: "brand" },
  completed: { label: "Completed", tone: "success" },
  no_show: { label: "No-show", tone: "warning" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

const NOTIFY_ERRORS: Record<string, string> = {
  window_closed:
    "The customer wasn't told on WhatsApp: they haven't messaged in the last 24 hours, so WhatsApp only allows an approved template. Please call them.",
  no_whatsapp_number: "The customer wasn't told: no WhatsApp number is connected.",
};

const REMINDER_DETAILS: Record<string, string> = {
  no_template: "no reminder template chosen and the customer hadn't messaged in 24 hours",
  no_whatsapp_number: "no WhatsApp number connected",
  template_has_too_many_variables: "the template uses more than 6 variables",
};

// ---- dates (all in the business's time zone, as YYYY-MM-DD strings) ----------------

function todayIn(tz: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}

function addDays(iso: string, n: number): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function mondayOf(iso: string): string {
  const weekday = (new Date(`${iso}T00:00:00Z`).getUTCDay() + 6) % 7;
  return addDays(iso, -weekday);
}

function dayLabel(iso: string, long = false): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, {
    weekday: long ? "long" : "short",
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  });
}

function time12(hhmm: string): string {
  const [h, m] = hhmm.split(":").map(Number);
  return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}

function hoursText(ranges: [string, string][] | undefined): string {
  return (ranges ?? []).map(([a, b]) => `${a}-${b}`).join(", ");
}

function parseHours(text: string): [string, string][] {
  return text
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const [a, b] = part.split(/\s*[-–to]+\s*/).filter(Boolean);
      return [a ?? "", b ?? ""] as [string, string];
    });
}

function hoursSummary(hours: WeeklyHours): string {
  const days = WEEKDAYS.filter((d) => hours[d.key]?.length);
  if (days.length === 0) return "No hours set";
  return days.map((d) => `${d.label.slice(0, 3)} ${hoursText(hours[d.key])}`).join(" · ");
}

// ---- page ---------------------------------------------------------------------------

export default function AppointmentsPage() {
  const { can } = useAuth();
  const canConfigure = can("appointments:configure");
  const [tab, setTab] = useState<Tab>("calendar");
  const [settings, setSettings] = useState<BookingSettings | null>(null);
  const [resources, setResources] = useState<BookableResource[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadSetup = useCallback(
    () =>
      Promise.all([api<BookingSettings>("/appointments/settings"), api<BookableResource[]>("/appointments/resources")]).then(
        ([s, r]) => {
          setSettings(s);
          setResources(r);
          setError(null);
        },
        (err) => setError(errorMessage(err)),
      ),
    [],
  );

  useEffect(() => {
    void Promise.resolve().then(loadSetup);
  }, [loadSetup]);

  if (!settings || !resources) return error ? <Alert tone="error">{error}</Alert> : <PageLoader />;

  const tabs: { value: Tab; label: string }[] = [
    { value: "calendar", label: "Calendar" },
    { value: "resources", label: "Doctors & services" },
    ...(canConfigure ? [{ value: "settings" as Tab, label: "Booking settings" }] : []),
  ];

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Appointments</h1>
        <p className="mt-1 text-sm text-muted">
          Your booking calendar. When AI booking is on, the WhatsApp assistant offers free times and books, moves or cancels
          appointments by itself.
        </p>
      </header>
      {error && <Alert tone="error">{error}</Alert>}
      {canConfigure && <SetupHint settings={settings} resources={resources} onGo={setTab} />}

      <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Appointments sections">
        {tabs.map((t) => (
          <button
            key={t.value}
            type="button"
            role="tab"
            aria-selected={tab === t.value}
            onClick={() => setTab(t.value)}
            className={`rounded-full px-4 py-1.5 text-sm font-semibold ${
              tab === t.value ? "bg-brand-soft text-brand-ink" : "text-muted hover:bg-surface-muted hover:text-ink"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "calendar" && <Calendar timezone={settings.timezone} resources={resources} />}
      {tab === "resources" && <Resources resources={resources} canConfigure={canConfigure} onChanged={loadSetup} />}
      {tab === "settings" && canConfigure && <Settings settings={settings} onSaved={setSettings} />}
    </div>
  );
}

function SetupHint({
  settings,
  resources,
  onGo,
}: {
  settings: BookingSettings;
  resources: BookableResource[];
  onGo: (t: Tab) => void;
}) {
  if (resources.filter((r) => r.active).length === 0) {
    return (
      <Alert tone="info">
        <p className="font-medium">Step 1: add your doctors or services.</p>
        <p className="mt-1">For each one, enter the days and hours they see patients and how long an appointment is.</p>
        <Button variant="secondary" className="mt-2 py-1" onClick={() => onGo("resources")}>
          Add a doctor or service
        </Button>
      </Alert>
    );
  }
  if (!settings.ai_booking_enabled) {
    return (
      <Alert tone="warning">
        <p className="font-medium">Step 2: let the WhatsApp assistant book.</p>
        <p className="mt-1">AI booking is off, so the assistant still hands booking requests to your team.</p>
        <Button variant="secondary" className="mt-2 py-1" onClick={() => onGo("settings")}>
          Turn on AI booking
        </Button>
      </Alert>
    );
  }
  return null;
}

// ---- calendar ------------------------------------------------------------------------

function Calendar({ timezone, resources }: { timezone: string; resources: BookableResource[] }) {
  const today = todayIn(timezone);
  const [weekStart, setWeekStart] = useState(() => mondayOf(today));
  const [resourceId, setResourceId] = useState("");
  const [showCancelled, setShowCancelled] = useState(false);
  const [list, setList] = useState<Appointment[] | null>(null);
  const [selected, setSelected] = useState<Appointment | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const days = useMemo(() => Array.from({ length: 7 }, (_, i) => addDays(weekStart, i)), [weekStart]);

  const load = useCallback(() => {
    const p = new URLSearchParams({ start: weekStart, end: addDays(weekStart, 6) });
    if (resourceId) p.set("resource_id", resourceId);
    if (!showCancelled) p.set("status", "active");
    return api<AppointmentList>(`/appointments?${p}`).then(
      (r) => {
        setList(r.items);
        setError(null);
      },
      (err) => setError(errorMessage(err)),
    );
  }, [weekStart, resourceId, showCancelled]);

  useEffect(() => {
    void Promise.resolve().then(load);
    const t = window.setInterval(load, 60_000); // the assistant books while you watch
    return () => window.clearInterval(t);
  }, [load]);

  const byDay = useMemo(() => {
    const map = new Map<string, Appointment[]>();
    for (const a of list ?? []) {
      const d = a.local_start.slice(0, 10);
      map.set(d, [...(map.get(d) ?? []), a]);
    }
    return map;
  }, [list]);

  const upcoming = (list ?? []).filter((a) => a.status === "confirmed");

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="secondary" className="px-3 py-1.5" onClick={() => setWeekStart(addDays(weekStart, -7))} aria-label="Previous week">
              ←
            </Button>
            <Button variant="secondary" className="px-3 py-1.5" onClick={() => setWeekStart(mondayOf(today))}>
              This week
            </Button>
            <Button variant="secondary" className="px-3 py-1.5" onClick={() => setWeekStart(addDays(weekStart, 7))} aria-label="Next week">
              →
            </Button>
            <span className="text-sm font-semibold">
              {dayLabel(days[0])} – {dayLabel(days[6])}
            </span>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-56">
              <SelectField
                label="Doctor or service"
                value={resourceId}
                onChange={(e) => setResourceId(e.target.value)}
                options={[{ value: "", label: "Everyone" }, ...resources.map((r) => ({ value: r.id, label: r.name }))]}
              />
            </div>
            <label className="flex items-center gap-2 pb-2.5 text-sm">
              <input type="checkbox" checked={showCancelled} onChange={(e) => setShowCancelled(e.target.checked)} />
              Show cancelled
            </label>
            <Button onClick={() => setCreating(true)} disabled={resources.filter((r) => r.active).length === 0}>
              New appointment
            </Button>
          </div>
        </div>
        <p className="mt-3 text-xs text-muted">
          {upcoming.length} confirmed this week · times are in {timezone}
        </p>
      </Card>
      {error && <Alert tone="error">{error}</Alert>}

      {creating && (
        <NewAppointment
          timezone={timezone}
          resources={resources.filter((r) => r.active)}
          onCancel={() => setCreating(false)}
          onCreated={(a) => {
            setCreating(false);
            setSelected(a);
            void load();
          }}
        />
      )}

      <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
        {list === null ? (
          <PageLoader />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-3 2xl:grid-cols-4">
            {days.map((d) => {
              const items = byDay.get(d) ?? [];
              return (
                <section
                  key={d}
                  className={`rounded-card border bg-surface p-3 shadow-card ${d === today ? "border-ring" : "border-line"}`}
                  aria-label={dayLabel(d, true)}
                >
                  <h3 className={`mb-2 text-sm font-semibold ${d === today ? "text-brand" : ""}`}>
                    {dayLabel(d)}
                    {d === today && <span className="ml-1 text-xs font-medium">(today)</span>}
                  </h3>
                  {items.length === 0 ? (
                    <p className="text-xs text-muted">No appointments</p>
                  ) : (
                    <ul className="space-y-1.5">
                      {items.map((a) => (
                        <li key={a.id}>
                          <button
                            type="button"
                            onClick={() => setSelected(a)}
                            className={`w-full rounded-control border px-2.5 py-2 text-left text-xs ${
                              selected?.id === a.id ? "border-ring bg-brand-soft" : "border-line hover:bg-surface-muted"
                            } ${a.status === "cancelled" ? "opacity-60 line-through" : ""}`}
                          >
                            <span className="block font-semibold tabular-nums">
                              {time12(a.local_start.slice(11, 16))} · {a.patient_name}
                            </span>
                            <span className="block text-muted">
                              {a.resource.name}
                              {a.source === "ai" && " · booked by AI"}
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
              );
            })}
          </div>
        )}
        {selected ? (
          <AppointmentPanel
            key={selected.id}
            appointment={selected}
            timezone={timezone}
            resources={resources}
            onChanged={(a) => {
              setSelected(a);
              void load();
            }}
            onClose={() => setSelected(null)}
          />
        ) : (
          <div className="hidden xl:block">
            <EmptyState title="Select an appointment" description="Pick one to see its details, move it, mark it done or cancel it." />
          </div>
        )}
      </div>
    </div>
  );
}

/** Free times for one doctor or service on one day, as buttons. */
function TimePicker({
  resourceId,
  date,
  value,
  onChange,
}: {
  resourceId: string;
  date: string;
  value: string;
  onChange: (t: string) => void;
}) {
  const [times, setTimes] = useState<string[] | null>(null);
  const [other, setOther] = useState(false);
  useEffect(() => {
    if (!resourceId || !date) return;
    let live = true;
    api<ResourceAvailability[]>(`/appointments/availability?start=${date}&days=1&resource_id=${resourceId}`).then(
      (r) => live && setTimes(r[0]?.days[0]?.times ?? []),
      () => live && setTimes([]),
    );
    return () => {
      live = false;
    };
  }, [resourceId, date]);

  return (
    <div className="space-y-2">
      <p className="text-sm font-medium">Time</p>
      {times === null ? (
        <p className="text-xs text-muted">Looking for free times…</p>
      ) : times.length === 0 ? (
        <p className="text-xs text-muted">No free times on this day. You can still enter a time below.</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {times.map((t) => (
            <button
              key={t}
              type="button"
              aria-pressed={value === t}
              onClick={() => {
                setOther(false);
                onChange(t);
              }}
              className={`rounded-full px-3 py-1 text-xs font-semibold tabular-nums ${
                value === t ? "bg-brand text-white" : "bg-surface-muted text-ink hover:bg-brand-soft"
              }`}
            >
              {time12(t)}
            </button>
          ))}
        </div>
      )}
      <label className="flex items-center gap-2 text-xs text-muted">
        <input type="checkbox" checked={other || (times !== null && times.length === 0)} onChange={(e) => setOther(e.target.checked)} />
        Another time (outside the listed free times)
      </label>
      {(other || (times !== null && times.length === 0)) && (
        <TextField label="Start time" type="time" value={value} onChange={(e) => onChange(e.target.value)} />
      )}
    </div>
  );
}

function NewAppointment({
  timezone,
  resources,
  onCancel,
  onCreated,
}: {
  timezone: string;
  resources: BookableResource[];
  onCancel: () => void;
  onCreated: (a: Appointment) => void;
}) {
  const [resourceId, setResourceId] = useState(resources[0]?.id ?? "");
  const [date, setDate] = useState(todayIn(timezone));
  const [time, setTime] = useState("");
  const [phone, setPhone] = useState("");
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [notify, setNotify] = useState(true);
  const [overbook, setOverbook] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const a = await api<AppointmentResult>("/appointments", {
        method: "POST",
        body: { resource_id: resourceId, phone, date, time, patient_name: name, notes, notify, allow_overbook: overbook },
      });
      if (a.notify_error) setWarning(NOTIFY_ERRORS[a.notify_error] ?? `The customer wasn't told on WhatsApp (${a.notify_error}).`);
      onCreated(a);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="New appointment" description="Book a patient who called or walked in.">
      <form onSubmit={submit} className="grid gap-4 md:grid-cols-2">
        <SelectField
          label="Doctor or service"
          value={resourceId}
          onChange={(e) => {
            setResourceId(e.target.value);
            setTime("");
          }}
          options={resources.map((r) => ({ value: r.id, label: r.department ? `${r.name} (${r.department})` : r.name }))}
        />
        <TextField
          label="Date"
          type="date"
          value={date}
          min={todayIn(timezone)}
          onChange={(e) => {
            setDate(e.target.value);
            setTime("");
          }}
          required
        />
        <div className="md:col-span-2">
          <TimePicker resourceId={resourceId} date={date} value={time} onChange={setTime} />
        </div>
        <TextField label="Patient name" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} required />
        <TextField
          label="WhatsApp number"
          placeholder="+919876543210"
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
          hint="With the country code. Reminders and updates go to this number."
          required
        />
        <div className="md:col-span-2">
          <TextAreaField label="Notes (optional)" value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={1000} placeholder="Reason for the visit" />
        </div>
        <div className="space-y-2 text-sm md:col-span-2">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={notify} onChange={(e) => setNotify(e.target.checked)} />
            Send the confirmation on WhatsApp (works if they messaged you in the last 24 hours)
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={overbook} onChange={(e) => setOverbook(e.target.checked)} />
            Book even if the time is already full
          </label>
        </div>
        {error && (
          <div className="md:col-span-2">
            <Alert tone="error">{error}</Alert>
          </div>
        )}
        {warning && (
          <div className="md:col-span-2">
            <Alert tone="warning">{warning}</Alert>
          </div>
        )}
        <div className="flex gap-2 md:col-span-2">
          <Button type="submit" loading={busy} disabled={!time || !resourceId}>
            Book appointment
          </Button>
          <Button type="button" variant="secondary" onClick={onCancel}>
            Close
          </Button>
        </div>
      </form>
    </Card>
  );
}

function AppointmentPanel({
  appointment: a,
  timezone,
  resources,
  onChanged,
  onClose,
}: {
  appointment: Appointment;
  timezone: string;
  resources: BookableResource[];
  onChanged: (a: Appointment) => void;
  onClose: () => void;
}) {
  const [moving, setMoving] = useState(false);
  const [moveResource, setMoveResource] = useState(a.resource.id);
  const [moveDate, setMoveDate] = useState(a.local_start.slice(0, 10));
  const [moveTime, setMoveTime] = useState("");
  const [notify, setNotify] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [openedAt] = useState(() => Date.now());

  async function act(label: string, path: string, method: "POST" | "PATCH", body: unknown) {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      const r = await api<AppointmentResult>(path, { method, body });
      if (r.notify_error) setNotice(NOTIFY_ERRORS[r.notify_error] ?? `The customer wasn't told on WhatsApp (${r.notify_error}).`);
      else if (r.notified) setNotice("The customer was told on WhatsApp.");
      setMoving(false);
      onChanged(r);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  const status = STATUS[a.status];
  const past = new Date(a.starts_at).getTime() < openedAt;
  return (
    <Card
      title={a.patient_name}
      description={`${dayLabel(a.local_start.slice(0, 10), true)}, ${time12(a.local_start.slice(11, 16))}`}
      actions={
        <Button variant="ghost" className="px-2 py-1" onClick={onClose} aria-label="Close">
          ✕
        </Button>
      }
    >
      <div className="space-y-4 text-sm">
        <div className="flex flex-wrap gap-1.5">
          <Badge tone={status.tone}>{status.label}</Badge>
          <Badge tone={a.source === "ai" ? "brand" : "neutral"}>{a.source === "ai" ? "Booked by AI" : "Booked by staff"}</Badge>
        </div>
        <dl className="grid grid-cols-[110px_1fr] gap-y-1.5">
          <dt className="text-muted">Reference</dt>
          <dd className="font-mono font-semibold">{a.reference}</dd>
          <dt className="text-muted">With</dt>
          <dd>
            {a.resource.name}
            {a.resource.department && <span className="text-muted"> · {a.resource.department}</span>}
          </dd>
          <dt className="text-muted">WhatsApp</dt>
          <dd>
            {a.contact.phone_e164 ?? "—"}
            {a.contact.name && a.contact.name !== a.patient_name && <span className="text-muted"> ({a.contact.name})</span>}
          </dd>
          {a.notes && (
            <>
              <dt className="text-muted">Notes</dt>
              <dd className="whitespace-pre-wrap">{a.notes}</dd>
            </>
          )}
          <dt className="text-muted">Reminder</dt>
          <dd>
            {a.reminder_status === "sent"
              ? `Sent ${formatDateTime(a.reminder_sent_at)}`
              : a.reminder_status
                ? `${a.reminder_status === "failed" ? "Failed" : "Not sent"}: ${REMINDER_DETAILS[a.reminder_detail ?? ""] ?? a.reminder_detail}`
                : "Not sent yet"}
          </dd>
          <dt className="text-muted">Booked</dt>
          <dd>
            {formatDateTime(a.created_at)}
            {a.created_by && ` by ${a.created_by.name}`}
          </dd>
          {a.cancelled_at && (
            <>
              <dt className="text-muted">Cancelled</dt>
              <dd>
                {formatDateTime(a.cancelled_at)} by {a.cancelled_by === "customer" ? "the customer on WhatsApp" : "staff"}
              </dd>
            </>
          )}
        </dl>
        {a.conversation_id && (
          <Link href={`/conversations?c=${a.conversation_id}`} className="inline-block font-semibold text-brand underline">
            Open the WhatsApp conversation
          </Link>
        )}
        {error && <Alert tone="error">{error}</Alert>}
        {notice && <Alert tone={notice.startsWith("The customer was told") ? "success" : "warning"}>{notice}</Alert>}

        {a.status !== "cancelled" && (
          <div className="space-y-3 border-t border-line pt-4">
            {moving ? (
              <div className="space-y-3">
                <SelectField
                  label="Doctor or service"
                  value={moveResource}
                  onChange={(e) => {
                    setMoveResource(e.target.value);
                    setMoveTime("");
                  }}
                  options={resources.filter((r) => r.active || r.id === a.resource.id).map((r) => ({ value: r.id, label: r.name }))}
                />
                <TextField
                  label="New date"
                  type="date"
                  value={moveDate}
                  min={todayIn(timezone)}
                  onChange={(e) => {
                    setMoveDate(e.target.value);
                    setMoveTime("");
                  }}
                />
                <TimePicker resourceId={moveResource} date={moveDate} value={moveTime} onChange={setMoveTime} />
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={notify} onChange={(e) => setNotify(e.target.checked)} />
                  Tell the customer on WhatsApp
                </label>
                <div className="flex gap-2">
                  <Button
                    loading={busy === "move"}
                    disabled={!moveTime}
                    onClick={() =>
                      act("move", `/appointments/${a.id}`, "PATCH", {
                        resource_id: moveResource,
                        date: moveDate,
                        time: moveTime,
                        notify,
                        allow_overbook: false,
                      })
                    }
                  >
                    Move appointment
                  </Button>
                  <Button variant="secondary" onClick={() => setMoving(false)}>
                    Back
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex flex-wrap gap-2">
                {a.status === "confirmed" && !past && (
                  <Button variant="secondary" onClick={() => setMoving(true)}>
                    Move
                  </Button>
                )}
                {a.status !== "completed" && (
                  <Button variant="secondary" loading={busy === "done"} onClick={() => act("done", `/appointments/${a.id}`, "PATCH", { status: "completed" })}>
                    Mark completed
                  </Button>
                )}
                {a.status !== "no_show" && past && (
                  <Button variant="secondary" loading={busy === "noshow"} onClick={() => act("noshow", `/appointments/${a.id}`, "PATCH", { status: "no_show" })}>
                    No-show
                  </Button>
                )}
                {a.status !== "confirmed" && (
                  <Button variant="ghost" loading={busy === "undo"} onClick={() => act("undo", `/appointments/${a.id}`, "PATCH", { status: "confirmed" })}>
                    Back to confirmed
                  </Button>
                )}
                {a.status === "confirmed" && (
                  <Button
                    variant="danger"
                    loading={busy === "cancel"}
                    onClick={() => {
                      if (window.confirm(`Cancel ${a.patient_name}'s appointment? The customer is told on WhatsApp if possible.`)) {
                        void act("cancel", `/appointments/${a.id}/cancel`, "POST", { notify: true });
                      }
                    }}
                  >
                    Cancel appointment
                  </Button>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

// ---- doctors and services --------------------------------------------------------------

function Resources({
  resources,
  canConfigure,
  onChanged,
}: {
  resources: BookableResource[];
  canConfigure: boolean;
  onChanged: () => Promise<void>;
}) {
  const [editing, setEditing] = useState<BookableResource | "new" | null>(null);
  return (
    <div className="space-y-4">
      <Card
        title="Doctors & services"
        description="Everyone patients can book with, and when. The assistant only offers times inside these hours."
        actions={canConfigure && <Button onClick={() => setEditing("new")}>Add doctor or service</Button>}
      >
        {editing && (
          <div className="mb-6">
            <ResourceForm
              key={editing === "new" ? "new" : editing.id}
              resource={editing === "new" ? null : editing}
              onDone={() => {
                setEditing(null);
                void onChanged();
              }}
              onCancel={() => setEditing(null)}
            />
          </div>
        )}
        {resources.length === 0 ? (
          <EmptyState title="No doctors or services yet" description="Add the first one to start taking bookings." />
        ) : (
          <ul className="divide-y divide-line">
            {resources.map((r) => (
              <li key={r.id} className="flex flex-wrap items-start justify-between gap-3 py-3">
                <div className="min-w-0">
                  <p className="font-semibold">
                    {r.name}
                    {r.department && <span className="font-normal text-muted"> · {r.department}</span>}
                    {!r.active && (
                      <span className="ml-2">
                        <Badge>Off</Badge>
                      </span>
                    )}
                  </p>
                  <p className="mt-0.5 text-xs text-muted">{hoursSummary(r.weekly_hours)}</p>
                  <p className="mt-0.5 text-xs text-muted">
                    {r.slot_minutes}-minute appointments
                    {r.capacity > 1 && ` · ${r.capacity} patients per time`} · {r.upcoming} upcoming
                  </p>
                </div>
                {canConfigure && (
                  <Button variant="secondary" className="py-1.5" onClick={() => setEditing(r)}>
                    Edit
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
      <TimeOffCard resources={resources} canConfigure={canConfigure} />
    </div>
  );
}

function ResourceForm({
  resource,
  onDone,
  onCancel,
}: {
  resource: BookableResource | null;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(resource?.name ?? "");
  const [department, setDepartment] = useState(resource?.department ?? "");
  const [description, setDescription] = useState(resource?.description ?? "");
  const [slot, setSlot] = useState(String(resource?.slot_minutes ?? 15));
  const [capacity, setCapacity] = useState(String(resource?.capacity ?? 1));
  const [active, setActive] = useState(resource?.active ?? true);
  const [hours, setHours] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      WEEKDAYS.map((d) => [d.key, resource ? hoursText(resource.weekly_hours[d.key]) : d.key === "sun" ? "" : "10:00-13:00, 17:00-20:00"]),
    ),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const weekly: WeeklyHours = {};
    for (const d of WEEKDAYS) {
      const ranges = parseHours(hours[d.key] ?? "");
      if (ranges.length) weekly[d.key] = ranges;
    }
    const body = {
      name,
      department,
      description,
      slot_minutes: Number(slot),
      capacity: Number(capacity),
      weekly_hours: weekly,
      active,
    };
    try {
      await api(resource ? `/appointments/resources/${resource.id}` : "/appointments/resources", {
        method: resource ? "PUT" : "POST",
        body,
      });
      onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!resource || !window.confirm(`Delete ${resource.name}?`)) return;
    setBusy(true);
    try {
      await api(`/appointments/resources/${resource.id}`, { method: "DELETE" });
      onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={save} className="space-y-4 rounded-control border border-line p-4">
      <div className="grid gap-4 md:grid-cols-2">
        <TextField label="Name" placeholder="Dr. Asha Rao, or X-ray" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} required />
        <TextField label="Department (optional)" placeholder="Cardiology" value={department} onChange={(e) => setDepartment(e.target.value)} maxLength={120} />
        <div className="md:col-span-2">
          <TextField
            label="Details for the assistant (optional)"
            placeholder="Heart specialist, MBBS MD. Consultation fee ₹500."
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            maxLength={500}
            hint="The assistant uses this to suggest the right doctor and to answer questions."
          />
        </div>
        <SelectField
          label="Appointment length"
          value={slot}
          onChange={(e) => setSlot(e.target.value)}
          options={[10, 15, 20, 30, 45, 60, 90, 120].map((m) => ({ value: String(m), label: `${m} minutes` }))}
        />
        <TextField
          label="Patients per time"
          type="number"
          min={1}
          max={50}
          value={capacity}
          onChange={(e) => setCapacity(e.target.value)}
          hint="1 for a doctor. More for a service that sees several people at once."
        />
      </div>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Weekly hours</legend>
        <p className="text-xs text-muted">
          24-hour clock, for example <span className="font-mono">10:00-13:00, 17:00-20:00</span>. Leave a day empty when closed.
        </p>
        {WEEKDAYS.map((d) => (
          <div key={d.key} className="grid grid-cols-[100px_1fr] items-center gap-3">
            <label htmlFor={`hours-${d.key}`} className="text-sm">
              {d.label}
            </label>
            <input
              id={`hours-${d.key}`}
              className="w-full rounded-control border-[1.5px] border-line bg-surface px-3 py-2 font-mono text-sm dark:bg-surface-muted"
              value={hours[d.key] ?? ""}
              placeholder="Closed"
              onChange={(e) => setHours((h) => ({ ...h, [d.key]: e.target.value }))}
            />
          </div>
        ))}
        <Button
          type="button"
          variant="ghost"
          className="px-2 py-1 text-xs"
          onClick={() => setHours((h) => Object.fromEntries(WEEKDAYS.map((d) => [d.key, d.key === "sun" ? h.sun ?? "" : h.mon ?? ""])))}
        >
          Copy Monday to Tuesday–Saturday
        </Button>
      </fieldset>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
        Taking appointments
      </label>
      {error && <Alert tone="error">{error}</Alert>}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" loading={busy}>
          {resource ? "Save changes" : "Add"}
        </Button>
        <Button type="button" variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
        {resource && (
          <Button type="button" variant="danger" onClick={remove} disabled={busy}>
            Delete
          </Button>
        )}
      </div>
    </form>
  );
}

function TimeOffCard({ resources, canConfigure }: { resources: BookableResource[]; canConfigure: boolean }) {
  const [rows, setRows] = useState<TimeOff[] | null>(null);
  const [who, setWho] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => api<TimeOff[]>("/appointments/time-off").then(setRows, (err) => setError(errorMessage(err))), []);
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/appointments/time-off", {
        method: "POST",
        body: { resource_id: who || null, starts_on: from, ends_on: to || from, reason },
      });
      setFrom("");
      setTo("");
      setReason("");
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Holidays and leave" description="Whole days when nobody, or one doctor, can be booked. Existing appointments on those days are kept.">
      {rows && rows.length > 0 ? (
        <ul className="mb-4 divide-y divide-line text-sm">
          {rows.map((r) => (
            <li key={r.id} className="flex items-center justify-between gap-3 py-2">
              <span>
                <span className="font-semibold">{r.resource_name ?? "Everyone"}</span> · {dayLabel(r.starts_on)}
                {r.ends_on !== r.starts_on && ` – ${dayLabel(r.ends_on)}`}
                {r.reason && <span className="text-muted"> · {r.reason}</span>}
              </span>
              {canConfigure && (
                <Button
                  variant="ghost"
                  className="px-2 py-1 text-xs"
                  onClick={() => api(`/appointments/time-off/${r.id}`, { method: "DELETE" }).then(load, (err) => setError(errorMessage(err)))}
                >
                  Remove
                </Button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mb-4 text-sm text-muted">No upcoming holidays or leave.</p>
      )}
      {canConfigure && (
        <form onSubmit={add} className="grid gap-3 md:grid-cols-4">
          <SelectField
            label="Who"
            value={who}
            onChange={(e) => setWho(e.target.value)}
            options={[{ value: "", label: "Everyone (closed)" }, ...resources.map((r) => ({ value: r.id, label: r.name }))]}
          />
          <TextField label="From" type="date" value={from} onChange={(e) => setFrom(e.target.value)} required />
          <TextField label="To (optional)" type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} />
          <TextField label="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={200} placeholder="Diwali" />
          <div className="md:col-span-4">
            <Button type="submit" variant="secondary" loading={busy}>
              Add days off
            </Button>
          </div>
        </form>
      )}
      {error && (
        <div className="mt-3">
          <Alert tone="error">{error}</Alert>
        </div>
      )}
    </Card>
  );
}

// ---- settings ----------------------------------------------------------------------------

function Settings({ settings, onSaved }: { settings: BookingSettings; onSaved: (s: BookingSettings) => void }) {
  const [form, setForm] = useState(settings);
  const [templates, setTemplates] = useState<MessageTemplate[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api<MessageTemplate[]>("/whatsapp/templates?approved_only=true").then(setTemplates, () => setTemplates([]));
  }, []);

  function set<K extends keyof BookingSettings>(key: K, value: BookingSettings[K]) {
    setSaved(false);
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { timezone: _tz, active_resources: _n, ...body } = form;
      void _tz;
      void _n;
      const s = await api<BookingSettings>("/appointments/settings", { method: "PUT", body });
      setForm(s);
      onSaved(s);
      setSaved(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const templateValue = form.reminder_template_name ? `${form.reminder_template_name}|${form.reminder_template_language ?? ""}` : "";

  return (
    <form onSubmit={save} className="space-y-4">
      <Card title="AI booking" description="Let the WhatsApp assistant book, move and cancel appointments itself, without handing over to your team.">
        <div className="space-y-4">
          <label className="flex items-start gap-3 text-sm">
            <input type="checkbox" className="mt-1" checked={form.ai_booking_enabled} onChange={(e) => set("ai_booking_enabled", e.target.checked)} />
            <span>
              <span className="font-semibold">The assistant books appointments</span>
              <span className="block text-muted">
                It asks which doctor, day and time and the patient&apos;s name, offers only free times, confirms the details, and
                sends a confirmation with a reference number. It needs the assistant&apos;s automatic replies to be on.
              </span>
            </span>
          </label>
          <div className="grid gap-4 md:grid-cols-3">
            <TextField
              label="Book up to (days ahead)"
              type="number"
              min={1}
              max={90}
              value={form.horizon_days}
              onChange={(e) => set("horizon_days", Number(e.target.value))}
            />
            <SelectField
              label="Earliest booking"
              value={String(form.min_notice_minutes)}
              onChange={(e) => set("min_notice_minutes", Number(e.target.value))}
              options={[
                { value: "0", label: "Any free time" },
                { value: "30", label: "30 minutes from now" },
                { value: "60", label: "1 hour from now" },
                { value: "120", label: "2 hours from now" },
                { value: "240", label: "4 hours from now" },
                { value: "1440", label: "From tomorrow" },
              ]}
            />
            <TextField
              label="Upcoming bookings per number"
              type="number"
              min={1}
              max={10}
              value={form.max_upcoming_per_contact}
              onChange={(e) => set("max_upcoming_per_contact", Number(e.target.value))}
              hint="Stops one number filling the calendar."
            />
          </div>
          <TextAreaField
            label="Added to every confirmation (optional)"
            placeholder="Please arrive 15 minutes early and bring your previous reports."
            value={form.confirmation_note}
            maxLength={500}
            onChange={(e) => set("confirmation_note", e.target.value)}
          />
        </div>
      </Card>

      <Card title="Reminders" description="A WhatsApp reminder before each appointment.">
        <div className="space-y-4">
          <label className="flex items-center gap-3 text-sm">
            <input type="checkbox" checked={form.reminders_enabled} onChange={(e) => set("reminders_enabled", e.target.checked)} />
            <span className="font-semibold">Send reminders</span>
          </label>
          <div className="grid gap-4 md:grid-cols-2">
            <SelectField
              label="When"
              value={String(form.reminder_hours_before)}
              onChange={(e) => set("reminder_hours_before", Number(e.target.value))}
              options={[2, 4, 12, 24, 48].map((h) => ({ value: String(h), label: `${h} hours before` }))}
            />
            <SelectField
              label="Approved template"
              value={templateValue}
              onChange={(e) => {
                const [name, language] = e.target.value.split("|");
                setSaved(false);
                setForm((f) => ({ ...f, reminder_template_name: name || null, reminder_template_language: language || null }));
              }}
              options={[
                { value: "", label: "None (only customers who wrote in the last 24 hours)" },
                ...templates.map((t) => ({ value: `${t.name}|${t.language}`, label: `${t.name} (${t.language})` })),
              ]}
            />
          </div>
          <Alert tone="info">
            <p>
              WhatsApp only lets businesses message a customer who hasn&apos;t written in the last 24 hours with a template that Meta
              has approved. Create a <b>Utility</b> template in WhatsApp Manager, for example:
            </p>
            <p className="mt-2 rounded-control bg-surface px-3 py-2 font-mono text-xs text-ink">
              Hello {"{{1}}"}, this is a reminder of your appointment with {"{{2}}"} on {"{{3}}"} at {"{{4}}"}. Your reference is {"{{5}}"}. Reply here
              to change or cancel it.
            </p>
            <p className="mt-2">
              The variables are filled in this order: 1 patient name, 2 doctor or service, 3 date, 4 time, 5 reference, 6 business
              name. Then sync templates on the{" "}
              <Link href="/whatsapp" className="font-semibold underline">
                WhatsApp page
              </Link>{" "}
              and choose it here.
            </p>
          </Alert>
        </div>
      </Card>

      {error && <Alert tone="error">{error}</Alert>}
      {saved && <Alert tone="success">Saved. The assistant uses these settings from its next reply.</Alert>}
      <Button type="submit" loading={busy}>
        Save booking settings
      </Button>
    </form>
  );
}
