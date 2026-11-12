// Thin fetch wrapper. The access token lives only in memory; the refresh token
// is an HttpOnly cookie the browser sends to /api/v1/auth/* automatically.

import type { SessionResponse } from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
  ) {
    super(message);
  }
}

const CSRF_HEADER = { "X-Requested-With": "XploWhatsApp" };

let accessToken: string | null = null;
let refreshInFlight: Promise<SessionResponse | null> | null = null;
let onSessionChange: ((s: SessionResponse | null) => void) | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function subscribeSession(cb: (s: SessionResponse | null) => void) {
  onSessionChange = cb;
}

async function parseError(res: Response): Promise<ApiError> {
  try {
    const body = await res.json();
    const e = body?.error ?? {};
    return new ApiError(res.status, e.code ?? `http_${res.status}`, e.message ?? res.statusText, e.details);
  } catch {
    return new ApiError(res.status, `http_${res.status}`, "Something went wrong. Please try again.");
  }
}

/** Uses the refresh cookie to get a new access token. Concurrent callers share one request. */
export function refreshSession(): Promise<SessionResponse | null> {
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      const res = await fetch("/api/v1/auth/refresh", {
        method: "POST",
        credentials: "same-origin",
        headers: CSRF_HEADER,
      });
      if (!res.ok) {
        setAccessToken(null);
        onSessionChange?.(null);
        return null;
      }
      const session = (await res.json()) as SessionResponse;
      setAccessToken(session.access_token);
      onSessionChange?.(session);
      return session;
    })().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

type Options = Omit<RequestInit, "body"> & { body?: unknown; auth?: boolean };

export async function api<T>(path: string, options: Options = {}, retry = true): Promise<T> {
  const { body, auth = true, headers, ...rest } = options;
  const res = await fetch(`/api/v1${path}`, {
    ...rest,
    credentials: "same-origin",
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(auth && accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...CSRF_HEADER,
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && auth && retry) {
    const refreshed = await refreshSession();
    if (refreshed) return api<T>(path, options, false);
  }
  if (!res.ok) throw await parseError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return "We couldn't reach the server. Check your connection and try again.";
}
