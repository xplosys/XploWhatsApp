// Mirrors the backend Pydantic schemas (app/modules/*/schemas.py).

export type TenantRole = "owner" | "admin" | "supervisor" | "agent";

export interface User {
  id: string;
  email: string;
  full_name: string;
  email_verified: boolean;
  mfa_enabled: boolean;
  is_super_admin: boolean;
  created_at: string;
}

export interface MembershipSummary {
  tenant_id: string;
  tenant_name: string;
  tenant_slug: string;
  role: TenantRole;
  status: string;
}

export interface SessionResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  user: User;
  active_tenant_id: string | null;
  memberships: MembershipSummary[];
}

export interface MfaChallenge {
  mfa_required: true;
  mfa_token: string;
}

export interface Me {
  user: User;
  active_tenant_id: string | null;
  role: TenantRole | null;
  permissions: string[];
  memberships: MembershipSummary[];
  mfa_verified: boolean;
}

export interface Tenant {
  id: string;
  name: string;
  slug: string;
  status: string;
  country: string;
  timezone: string;
  default_language: string;
  industry: string | null;
  settings: Record<string, unknown>;
  created_at: string;
}

export interface Member {
  id: string;
  user_id: string;
  email: string;
  full_name: string;
  role: TenantRole;
  status: "active" | "suspended";
  created_at: string;
}

export interface Invitation {
  id: string;
  email: string;
  role: TenantRole;
  expires_at: string;
  accepted_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface InvitationPreview {
  tenant_name: string;
  email: string;
  role: TenantRole;
  expires_at: string;
}

export interface Subscription {
  status: string | null;
  plan_code: string | null;
  plan_name: string | null;
  limits: Record<string, number>;
  trial_ends_at: string | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  seats_used: number;
  seats_included: number;
}

export interface AdminTenant {
  id: string;
  name: string;
  slug: string;
  status: string;
  country: string;
  created_at: string;
  member_count: number;
  plan_code: string | null;
  subscription_status: string | null;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
