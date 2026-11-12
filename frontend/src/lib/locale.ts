export const COUNTRIES = [
  { value: "IN", label: "India" },
  { value: "GB", label: "United Kingdom" },
  { value: "US", label: "United States" },
  { value: "AE", label: "United Arab Emirates" },
];

const DEFAULT_TZ: Record<string, string> = {
  IN: "Asia/Kolkata",
  GB: "Europe/London",
  US: "America/New_York",
  AE: "Asia/Dubai",
};

export function timezoneFor(country: string): string {
  try {
    const browser = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (country === "US" && browser.startsWith("America/")) return browser;
  } catch {
    /* fall through */
  }
  return DEFAULT_TZ[country] ?? "UTC";
}

export const TIMEZONES = [
  "Asia/Kolkata",
  "Asia/Dubai",
  "Europe/London",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "UTC",
];

export const LANGUAGES = [
  { value: "en", label: "English" },
  { value: "hi", label: "Hindi" },
];

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}
