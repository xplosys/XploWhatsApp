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

// ---- WhatsApp (Phase 2) ----

export interface SignupConfig {
  enabled: boolean;
  app_id: string | null;
  config_id: string | null;
  graph_api_version: string;
  embedded_signup_version: string;
  missing: string[];
}

export interface HealthIssue {
  code: string;
  level: "error" | "warning" | "info";
  message: string;
  at: string;
}

export interface WhatsAppNumber {
  id: string;
  phone_number_id: string;
  display_phone_number: string | null;
  verified_name: string | null;
  quality_rating: string | null;
  messaging_limit_tier: string | null;
  name_status: string | null;
  meta_status: string | null;
  status: "connected" | "pending" | "disconnected";
  is_coexistence: boolean;
  registered_at: string | null;
}

export interface WhatsAppAccount {
  id: string;
  waba_id: string;
  meta_business_id: string | null;
  name: string | null;
  currency: string | null;
  status: "connected" | "needs_reconnect" | "disconnected";
  health: "ok" | "warning" | "error" | "unknown";
  health_issues: HealthIssue[];
  last_health_check_at: string | null;
  connected_at: string | null;
  disconnected_at: string | null;
  templates_synced_at: string | null;
  numbers: WhatsAppNumber[];
}

export interface MessageTemplate {
  id: string;
  account_id: string;
  name: string;
  language: string;
  category: string | null;
  status: string;
  rejected_reason: string | null;
  components: { type: string; text?: string; format?: string }[];
  last_synced_at: string | null;
}

export interface Contact {
  id: string;
  wa_id: string | null;
  phone_e164: string | null;
  bsuid: string | null;
  username: string | null;
  profile_name: string | null;
  name: string | null;
  consent_status: "unknown" | "opted_in" | "opted_out";
  consent_updated_at: string | null;
  last_inbound_at: string | null;
}

export interface Conversation {
  id: string;
  contact: Contact;
  number_id: string;
  number_display: string | null;
  status: "open" | "closed";
  unread_count: number;
  last_inbound_at: string | null;
  last_message_at: string | null;
  service_window_expires_at: string | null;
  service_window_open: boolean;
  last_message_preview: string | null;
  ai_enabled: boolean;
  needs_human: boolean;
}

export interface ChatMessage {
  id: string;
  conversation_id: string;
  direction: "inbound" | "outbound";
  sender_type: "contact" | "agent" | "ai" | "system";
  type: string;
  body: string | null;
  status: string;
  error_code: string | null;
  error_message: string | null;
  pricing_category: string | null;
  wa_timestamp: string | null;
  created_at: string;
  ai_provenance: {
    provider: string | null;
    model: string | null;
    reason: string;
    out_of_scope: boolean;
    sources?: KnowledgeSource[];
  } | null;
}

// ---- AI (Phase 3) ----

export type ProviderKey = "gemini" | "openai" | "deepseek";

export interface AIProvider {
  provider: ProviderKey;
  label: string;
  enabled: boolean;
  key_source: "tenant" | "platform";
  has_own_key: boolean;
  key_hint: string | null;
  platform_key_available: boolean;
  ready: boolean;
  last_test_at: string | null;
  last_test_ok: boolean | null;
  last_error: string | null;
}

export interface AIModel {
  id: string;
  provider: ProviderKey;
  model_id: string;
  display_name: string | null;
  context_window: number | null;
  max_output_tokens: number | null;
}

export interface ModelChoice {
  provider: ProviderKey;
  model: string;
}

export interface AssistantConfig {
  assistant_name: string;
  business_description: string;
  instructions: string;
  business_hours: string;
  tone: "friendly" | "professional" | "concise";
  reply_language: "match_customer" | "business_default";
  handover_message: string;
  max_reply_chars: number;
  primary: ModelChoice;
  fallbacks: ModelChoice[];
}

export interface AssistantVersion {
  version: number;
  config: AssistantConfig;
  created_at: string;
  created_by_user_id: string | null;
}

export interface Assistant {
  id: string;
  name: string;
  auto_reply_enabled: boolean;
  active_version: AssistantVersion | null;
  warnings: string[];
}

export interface PreviewResult {
  kind: "reply" | "handover";
  text: string;
  reason: string;
  provider: string | null;
  model: string | null;
  out_of_scope: boolean;
  attempts: { provider: string; model: string; outcome: string; detail: string | null }[];
  sources: KnowledgeSource[];
  retrieval: { hits: number; status?: string; error?: string } | null;
}

/** A knowledge passage an AI answer was given, as kept in its provenance. */
export interface KnowledgeSource {
  document_id: string;
  chunk_id: string;
  title: string;
  score: number;
  pages?: number[];
  sheet?: string;
}

export interface AIUsage {
  period_start: string;
  ai_replies_used: number;
  ai_replies_limit: number;
  platform_keys_allowed: boolean;
  rows: {
    provider: string;
    model: string;
    calls: number;
    failed_calls: number;
    input_tokens: number;
    output_tokens: number;
    cost_estimate: string | null;
    currency: string | null;
    unpriced_calls: number;
  }[];
}

export interface AIPrice {
  id: string;
  input_per_mtok: string;
  output_per_mtok: string;
  cached_input_per_mtok: string | null;
  currency: string;
  effective_from: string;
  source_note: string | null;
}

export interface AdminAIModel extends AIModel {
  kind: "chat" | "embedding";
  active: boolean;
  last_seen_at: string | null;
  current_price: AIPrice | null;
}

// ---- knowledge base (Phase 4) ----

export interface EmbeddingModel {
  id: string;
  provider: ProviderKey;
  model_id: string;
  display_name: string | null;
}

export interface KnowledgeSettings {
  embedding_model: EmbeddingModel | null;
  pending_embedding_model: EmbeddingModel | null;
  reindex_error: string | null;
  reindex_done: number;
  reindex_total: number;
  min_score: string;
  top_k: number;
  usage: { documents: number; max_documents: number; storage_bytes: number; max_storage_bytes: number };
  storage_ready: boolean;
  virus_scan: boolean;
  max_file_mb: number;
}

export type VersionStatus = "queued" | "processing" | "ready" | "replaced" | "failed" | "rejected";

export interface KnowledgeVersion {
  version: number;
  status: VersionStatus;
  original_filename: string | null;
  file_type: string | null;
  size_bytes: number;
  scan_result: string | null;
  error_code: string | null;
  error: string | null;
  char_count: number;
  chunk_count: number;
  created_at: string;
  processed_at: string | null;
  downloadable: boolean;
}

export interface KnowledgeDocument {
  id: string;
  title: string;
  source_type: "file" | "url" | "text";
  source_url: string | null;
  active_version: number | null;
  latest: KnowledgeVersion | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeDocumentDetail extends KnowledgeDocument {
  versions: KnowledgeVersion[];
}

export interface KnowledgeChunk {
  ordinal: number;
  heading: string | null;
  content: string;
  meta: { pages?: number[]; sheet?: string; rows?: string };
}

export interface KnowledgeSearch {
  passages: { document_id: string; title: string; content: string; score: number; meta: KnowledgeChunk["meta"]; used: boolean }[];
  min_score: number;
  status: string;
}
