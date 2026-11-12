"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { api, refreshSession, setAccessToken, subscribeSession } from "@/lib/api/client";
import type { Me, MfaChallenge, SessionResponse } from "@/lib/api/types";

type Status = "loading" | "authenticated" | "anonymous";

interface AuthState {
  status: Status;
  me: Me | null;
  can: (permission: string) => boolean;
  /** Returns an MFA challenge when the account has two-factor authentication. */
  login: (email: string, password: string) => Promise<MfaChallenge | null>;
  verifyMfa: (mfaToken: string, code: string) => Promise<void>;
  acceptSession: (session: SessionResponse) => Promise<void>;
  switchTenant: (tenantId: string) => Promise<void>;
  reload: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<Status>("loading");
  const [me, setMe] = useState<Me | null>(null);

  const reload = useCallback(async () => {
    try {
      setMe(await api<Me>("/auth/me"));
      setStatus("authenticated");
    } catch {
      setMe(null);
      setStatus("anonymous");
    }
  }, []);

  const acceptSession = useCallback(
    async (session: SessionResponse) => {
      setAccessToken(session.access_token);
      await reload();
    },
    [reload],
  );

  useEffect(() => {
    subscribeSession((s) => {
      if (!s) {
        setMe(null);
        setStatus("anonymous");
      }
    });
    // Restore the session from the refresh cookie on first load.
    refreshSession().then((s) => (s ? reload() : setStatus("anonymous")));
  }, [reload]);

  const login = useCallback(
    async (email: string, password: string) => {
      const res = await api<SessionResponse | MfaChallenge>("/auth/login", {
        method: "POST",
        body: { email, password },
        auth: false,
      });
      if ("mfa_required" in res) return res;
      await acceptSession(res);
      return null;
    },
    [acceptSession],
  );

  const verifyMfa = useCallback(
    async (mfaToken: string, code: string) => {
      const session = await api<SessionResponse>("/auth/mfa/verify", {
        method: "POST",
        body: { mfa_token: mfaToken, code },
        auth: false,
      });
      await acceptSession(session);
    },
    [acceptSession],
  );

  const switchTenant = useCallback(
    async (tenantId: string) => {
      const session = await api<SessionResponse>("/auth/switch-tenant", {
        method: "POST",
        body: { tenant_id: tenantId },
      });
      await acceptSession(session);
    },
    [acceptSession],
  );

  const logout = useCallback(async () => {
    try {
      await api("/auth/logout", { method: "POST", auth: false });
    } finally {
      setAccessToken(null);
      setMe(null);
      setStatus("anonymous");
    }
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      status,
      me,
      can: (p) => !!me?.permissions.includes(p),
      login,
      verifyMfa,
      acceptSession,
      switchTenant,
      reload,
      logout,
    }),
    [status, me, login, verifyMfa, acceptSession, switchTenant, reload, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
