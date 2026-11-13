import type { Contact, InboxView, LeadStatus } from "@/lib/api/types";

export function contactLabel(c: Pick<Contact, "name" | "profile_name" | "phone_e164" | "username">): string {
  return c.name || c.profile_name || c.phone_e164 || (c.username ? `@${c.username}` : null) || "WhatsApp user";
}

export const INBOX_VIEWS: { value: InboxView; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "mine", label: "Mine" },
  { value: "unassigned", label: "Unassigned" },
  { value: "needs_human", label: "Needs a person" },
  { value: "awaiting", label: "Waiting for reply" },
  { value: "closed", label: "Closed" },
];

export const HANDOVER_REASON: Record<string, string> = {
  needs_human: "The assistant asked for a person",
  all_failed: "The assistant could not give a checked answer",
  agent_takeover: "Taken over by a team member",
  ai_turned_off: "AI replies turned off",
};

export function handoverReason(reason: string): string {
  if (reason.startsWith("blocked:")) return "The assistant's reply was blocked by the sending rules";
  return HANDOVER_REASON[reason] ?? reason;
}

export const LEAD_STATUSES: { value: LeadStatus; label: string; tone: "neutral" | "brand" | "warning" | "success" | "danger" }[] = [
  { value: "new", label: "New", tone: "brand" },
  { value: "contacted", label: "Contacted", tone: "neutral" },
  { value: "qualified", label: "Qualified", tone: "warning" },
  { value: "won", label: "Won", tone: "success" },
  { value: "lost", label: "Lost", tone: "danger" },
];

export function leadStatus(status: LeadStatus) {
  return LEAD_STATUSES.find((s) => s.value === status) ?? LEAD_STATUSES[0];
}

export function formatMoney(value: string | null, currency: string | null): string {
  if (value === null) return "—";
  const n = Number(value);
  if (!currency) return n.toLocaleString();
  try {
    return n.toLocaleString(undefined, { style: "currency", currency, maximumFractionDigits: n % 1 ? 2 : 0 });
  } catch {
    return `${currency} ${n.toLocaleString()}`;
  }
}
