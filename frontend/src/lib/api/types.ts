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
  /** The active business requires two-factor sign-in and this session doesn't have it. */
  mfa_required: boolean;
}

/** A device or browser where the account is signed in. */
export interface LoginSession {
  id: string;
  signed_in_at: string;
  last_active_at: string;
  ip: string | null;
  user_agent: string | null;
  current: boolean;
}

export interface SecuritySettings {
  require_mfa: boolean;
  message_retention_days: number | null;
  members_without_mfa: number;
}

export interface DataExport {
  id: string;
  status: "queued" | "running" | "ready" | "failed" | "expired";
  size_bytes: number | null;
  error: string | null;
  created_at: string;
  completed_at: string | null;
  expires_at: string | null;
}

export interface DeletedTenant {
  id: string;
  name: string;
  slug: string;
  deleted_at: string;
  purge_after: string | null;
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
  language: string | null;
  tags: string[];
  consent_status: "unknown" | "opted_in" | "opted_out";
  consent_source: string | null;
  consent_updated_at: string | null;
  last_inbound_at: string | null;
  created_at: string;
}

export interface UserRef {
  id: string;
  name: string;
}

export interface Handover {
  started_by: "ai" | "agent" | "system";
  reason: string;
  started_at: string;
  agent: UserRef | null;
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
  awaiting_reply: boolean;
  assigned_to: UserRef | null;
  assigned_at: string | null;
  closed_at: string | null;
  handover: Handover | null;
}

export type InboxView = "open" | "mine" | "unassigned" | "needs_human" | "awaiting" | "closed";

export type InboxCounts = Record<InboxView | "unread", number>;

export interface ConversationNote {
  id: string;
  body: string;
  author: UserRef | null;
  created_at: string;
  updated_at: string;
  can_edit: boolean;
  can_delete: boolean;
}

export interface ConversationSummary {
  status: "queued" | "ready" | "failed";
  summary: string | null;
  error: string | null;
  provider: string | null;
  model: string | null;
  generated_at: string | null;
  message_count: number;
  new_messages: number;
}

export interface HandoverHistory {
  id: string;
  started_by: "ai" | "agent" | "system";
  reason: string;
  started_at: string;
  agent: UserRef | null;
  released_at: string | null;
  released_by: UserRef | null;
}

export interface ContactDetail extends Contact {
  conversations: {
    id: string;
    number_id: string;
    number_display: string | null;
    status: "open" | "closed";
    last_message_at: string | null;
    assigned_to: UserRef | null;
    ai_enabled: boolean;
    needs_human: boolean;
  }[];
  leads: {
    id: string;
    title: string;
    status: LeadStatus;
    value: string | null;
    currency: string | null;
    owner: UserRef | null;
    updated_at: string;
  }[];
}

export interface TagCount {
  tag: string;
  contacts: number;
}

// ---- Leads (Phase 5) ----

export type LeadStatus = "new" | "contacted" | "qualified" | "won" | "lost";

export interface Lead {
  id: string;
  title: string;
  status: LeadStatus;
  source: "whatsapp" | "manual";
  value: string | null;
  currency: string | null;
  contact: { id: string; name: string | null; phone_e164: string | null };
  conversation_id: string | null;
  owner: UserRef | null;
  created_by: UserRef | null;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
}

export interface LeadActivity {
  id: string;
  type: "created" | "status_changed" | "owner_changed" | "value_changed" | "title_changed" | "note";
  data: Record<string, string | null>;
  actor: UserRef | null;
  created_at: string;
}

export interface LeadDetail extends Lead {
  activities: LeadActivity[];
}

export interface LeadSummary {
  by_status: { status: LeadStatus; count: number }[];
  open_value: { currency: string; total: string }[];
  mine_open: number;
  unowned_open: number;
  default_currency: string | null;
}

export interface UnansweredQuestion {
  id: string;
  conversation_id: string;
  message_id: string;
  contact_name: string | null;
  question: string;
  reason: string;
  retrieval_hits: number | null;
  status: "open" | "resolved" | "dismissed";
  resolved_by: UserRef | null;
  resolved_at: string | null;
  created_at: string;
}

export interface UnansweredPage extends Page<UnansweredQuestion> {
  open: number;
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
  summary_on_handover: boolean;
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

// ---- analytics -----------------------------------------------------------------------

export type AnalyticsPeriod = 7 | 30 | 90;

export interface AnalyticsTotals {
  conversations_started: number;
  messages_in: number;
  messages_ai: number;
  messages_team: number;
  ai_conversations: number;
  ai_handovers: number;
  ai_resolution_rate: number | null;
  leads_created: number;
  leads_won: number;
}

export interface AnalyticsDay {
  date: string;
  customer: number;
  ai: number;
  team: number;
}

export interface ReplyTime {
  conversations: number;
  median_seconds: number | null;
  p90_seconds: number | null;
}

export interface AnalyticsAgent {
  user_id: string;
  name: string;
  role: TenantRole | null;
  messages: number;
  conversations: number;
  handovers: number;
  handovers_answered: number;
  handover_median_seconds: number | null;
  leads_won: number;
}

export interface MoneyTotal {
  currency: string;
  total: string;
}

export interface AnalyticsOverview {
  days: AnalyticsPeriod;
  timezone: string;
  start: string;
  end: string;
  totals: AnalyticsTotals;
  previous: AnalyticsTotals;
  daily: AnalyticsDay[];
  hours: { weekday: number; hour: number; messages: number }[];
  handover_reasons: { reason: string; count: number }[];
  response: {
    first_reply_ai: ReplyTime | null;
    first_reply_team: ReplyTime | null;
    first_reply_none: number;
    handovers: number;
    handovers_answered: number;
    handover_median_seconds: number | null;
  };
  agents: AnalyticsAgent[];
  won_value: MoneyTotal[];
  open_unanswered: number;
  delivery: {
    sent: number;
    delivered: number;
    read: number;
    failed: number;
    pending: number;
    top_errors: { code: string; count: number }[];
    billable: { category: string; messages: number }[];
  };
  ai_cost: {
    calls: number;
    unpriced_calls: number;
    cost: MoneyTotal[];
    calls_by_purpose: { purpose: string; calls: number }[];
  } | null;
}

export interface SetupProgress {
  whatsapp_connected: boolean;
  assistant_published: boolean;
  auto_reply_on: boolean;
  knowledge_documents: number;
  active_members: number;
}

// ---- billing ---------------------------------------------------------------------

export type BillingInterval = "month" | "year";

export interface PlanPrice {
  id: string;
  currency: string;
  interval: BillingInterval;
  amount: string;
  seat_amount: string | null;
  tax_rate: string;
  amount_with_tax: string;
  gateway: "razorpay" | "stripe";
}

export interface BillingPlan {
  id: string;
  code: string;
  name: string;
  description: string | null;
  features: string[];
  limits: Record<string, number>;
  prices: PlanPrice[];
}

export interface BillingPlans {
  currencies: string[];
  plans: BillingPlan[];
  payments_ready: boolean;
}

export type SubscriptionStatus = "trialing" | "active" | "past_due" | "canceled" | "expired";

export interface BillingSubscription {
  status: SubscriptionStatus | null;
  in_force: boolean;
  plan_code: string | null;
  plan_name: string | null;
  limits: Record<string, number>;
  price: PlanPrice | null;
  pending_price: PlanPrice | null;
  pending_plan_name: string | null;
  trial_ends_at: string | null;
  current_period_start: string | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  past_due_since: string | null;
  grace_ends_at: string | null;
  extra_seats: number;
  seats_used: number;
  seats_included: number;
  gateway: "razorpay" | "stripe" | null;
}

export interface BillingUsage {
  key: string;
  label: string;
  used: number;
  limit: number;
}

export interface BillingProfile {
  legal_name: string;
  email: string | null;
  address_line1: string | null;
  address_line2: string | null;
  city: string | null;
  state: string | null;
  state_code: string | null;
  postal_code: string | null;
  country: string;
  gstin: string | null;
  tax_id: string | null;
  updated_at: string;
}

export interface GstState {
  code: string;
  name: string;
}

export interface CheckoutStart {
  checkout_id: string;
  gateway: "razorpay" | "stripe";
  razorpay: Record<string, unknown> | null;
  stripe_url: string | null;
}

export interface InvoiceSummary {
  id: string;
  number: string;
  currency: string;
  subtotal: string;
  tax_total: string;
  total: string;
  issued_at: string;
  period_start: string | null;
  period_end: string | null;
}

export interface InvoiceLine {
  description: string;
  sac: string | null;
  quantity: number;
  amount: string;
}

export interface InvoiceTax {
  name: string;
  rate: string;
  amount: string;
}

export interface Invoice extends InvoiceSummary {
  line_items: InvoiceLine[];
  taxes: InvoiceTax[];
  tax_note: string | null;
  seller: Record<string, string | null>;
  buyer: Record<string, string | null>;
  place_of_supply: string | null;
  gateway: string;
  gateway_payment_id: string;
}

export interface BillingTransaction {
  id: string;
  amount: string;
  currency: string;
  status: "succeeded" | "failed" | "refunded";
  method: string | null;
  failure_reason: string | null;
  invoice_id: string | null;
  occurred_at: string;
}

export interface AdminPlanPrice extends PlanPrice {
  gateway_plan_id: string | null;
  gateway_seat_price_id: string | null;
  is_active: boolean;
  created_at: string;
}

export interface AdminPlan {
  id: string;
  code: string;
  name: string;
  description: string | null;
  limits: Record<string, number>;
  features: string[];
  trial_days: number;
  is_public: boolean;
  is_active: boolean;
  sort_order: number;
  subscribers: number;
  prices: AdminPlanPrice[];
}
