export interface ForwardRule {
  id: string
  session_id: string
  source_chat_id: string
  source_chat_name: string
  target_chat_id: string
  target_chat_name: string
  routing_type: string
  forward_mode: string
  is_active: boolean
  is_paused: boolean
  priority: number
  message_category: string
  use_intermediate: boolean
  intermediate_channel_id: string
  intermediate_channel_name: string
  fallback_mode: string
  fallback_enabled: boolean
  detection_criteria: {
    match_mode?: 'ALL' | 'ANY'
    text_contains?: string[]
    keywords?: string[]
    regex_pattern?: string
    text_regex?: string
    forward_origin_chat_ids?: string[]
    forward_origin_titles?: string[]
    [key: string]: any
  }
  link_policy: string
  // ---- v17: full per-task control surface -------------------------------
  // Multi-source: the task forwards from its primary source_chat_id plus every
  // id here, so one task can cover several channels with one set of settings.
  source_chat_ids?: string[]
  source_chat_names?: string[]
  // Multi-target: mirror of the source list on the destination side. The task
  // fans out into every id here; target_chat_id stays authoritative and is
  // always the first entry of the list.
  target_chat_ids?: string[]
  target_chat_names?: string[]
  content_mode?: string
  skip_history?: boolean
  since_ts?: number
  ignore_edits?: boolean
  trigger_events?: string[]
  topic_id?: string
  ai_secondary_config_id?: string | null
  allowed_media_types?: string[]
  album_aggregation_window_seconds?: number
  intermediate_target_chat_id?: string
  intermediate_mode?: string
  retry_policy?: Record<string, any>
  proxy?: Record<string, any> | null
  // Every per-task setting the server knows about, keyed by name. Read-only
  // here: write through updateRuleSettings so values are validated server-side.
  settings?: Record<string, any>
  custom_header: string
  custom_footer: string
  split_long_caption: boolean
  filter_rule_id?: string | null
  ai_config_id?: string | null
  prompt_id?: string | null
  remove_links?: boolean
  delay_seconds?: number
  rate_limit_per_minute?: number
  max_retries?: number
  replacements?: Record<string, string>
  domain_allowlist?: string[]
  domain_blocklist?: string[]
  link_rewrite_map?: Record<string, string>
  // ---- v19: per-task content rules (the server's `content` group) --------
  // Mirrors core/domain/rule_settings.py. `settings` stays the authoritative
  // read/write path; these optional fields exist so a rule payload carrying the
  // content policy (e.g. from backup/restore) is typed, not `any`.
  content_policy_enabled?: boolean
  blacklist_words?: string[]
  whitelist_words?: string[]
  whitelist_mode?: string
  strip_words?: string[]
  regex_block?: string[]
  regex_require?: string[]
  source_policies?: Record<string, any>
  target_policies?: Record<string, any>
  created_at: number
  updated_at: number
  custom_metadata_json?: string
  name?: string
  debug_mode?: boolean
  account_type?: string
  sender_type?: string
  feature_group?: string
  duplicate_filters?: boolean
  cleaner_enabled?: boolean
  cleaner_config?: {
    patterns?: string[]
    use_regex?: boolean
    remove_urls?: boolean
    remove_mentions?: boolean
    remove_hashtags?: boolean
    remove_emojis?: boolean
    apply_to_caption?: boolean
    case_sensitive?: boolean
  }
  show_forward_header?: boolean
  sync_replies?: boolean
  sync_edits?: boolean
  sync_deletes?: boolean
  block_text?: boolean
  block_links?: boolean
  block_voice?: boolean
  block_stickers?: boolean
  block_video?: boolean
  block_audio?: boolean
  crypto_mode?: boolean
  whitelist_keywords?: string[]
  blacklist_keywords?: string[]
  whitelist_users?: string[]
  blacklist_users?: string[]
  auto_posting?: boolean
  power_control?: boolean
  forward_delay_seconds?: number
  edit_time_limit_seconds?: number
  process_raw_text?: boolean
  button_to_text?: boolean
  watermark_text?: string
  link_buttons?: Array<{ text: string; url: string }>
  affiliate_kit?: { enabled: boolean; prefix?: string; replace_urls?: boolean }
  ocr_image_ai?: boolean
  smart_ai_rewrite?: boolean
  news_digest?: boolean
  ai_creativity?: number
  ai_max_tokens?: number
  paid_addons?: Record<string, boolean>
  metadata?: Record<string, any>
}

export interface SaveRuleRequest {
  id?: string
  session_id?: string
  source_chat_id: string
  source_chat_name?: string
  target_chat_id: string
  target_chat_name?: string
  routing_type?: string
  forward_mode?: string
  is_active?: boolean
  is_paused?: boolean
  priority?: number
  message_category?: string
  use_intermediate?: boolean
  intermediate_channel_id?: string
  intermediate_channel_name?: string
  fallback_mode?: string
  fallback_enabled?: boolean
  detection_criteria?: any
  link_policy?: string
  custom_header?: string
  custom_footer?: string
  split_long_caption?: boolean
  filter_rule_id?: string | null
  ai_config_id?: string | null
  prompt_id?: string | null
  remove_links?: boolean
  delay_seconds?: number
  rate_limit_per_minute?: number
  max_retries?: number
  replacements?: Record<string, string>
  domain_allowlist?: string[]
  domain_blocklist?: string[]
  link_rewrite_map?: Record<string, string>
  // Multi-source / multi-target (mirrors of the single-value fields above).
  source_chat_ids?: string[]
  source_chat_names?: string[]
  target_chat_ids?: string[]
  target_chat_names?: string[]
  // Per-task content rules (the server's `content` group).
  content_policy_enabled?: boolean
  blacklist_words?: string[]
  whitelist_words?: string[]
  whitelist_mode?: string
  strip_words?: string[]
  regex_block?: string[]
  regex_require?: string[]
  source_policies?: Record<string, any>
  target_policies?: Record<string, any>
  custom_metadata_json?: string
  ai_fallback_policy?: string
  ai_prompt_version?: number
  ai_timeout_seconds?: number
  name?: string
  // Canonical type is string: proto/autoforward.proto declares `string topic_id = 61`,
  // and the core stores/reads it as a string (see _V17_STRS in servicers.py).
  topic_id?: string
  debug_mode?: boolean
  account_type?: string
  sender_type?: string
  feature_group?: string
  duplicate_filters?: boolean
  cleaner_enabled?: boolean
  cleaner_config?: {
    patterns?: string[]
    use_regex?: boolean
    remove_urls?: boolean
    remove_mentions?: boolean
    remove_hashtags?: boolean
    remove_emojis?: boolean
    apply_to_caption?: boolean
    case_sensitive?: boolean
  }
  show_forward_header?: boolean
  sync_replies?: boolean
  sync_edits?: boolean
  sync_deletes?: boolean
  block_text?: boolean
  block_links?: boolean
  block_voice?: boolean
  block_stickers?: boolean
  block_video?: boolean
  block_audio?: boolean
  crypto_mode?: boolean
  whitelist_keywords?: string[]
  blacklist_keywords?: string[]
  whitelist_users?: string[]
  blacklist_users?: string[]
  auto_posting?: boolean
  power_control?: boolean
  forward_delay_seconds?: number
  edit_time_limit_seconds?: number
  process_raw_text?: boolean
  button_to_text?: boolean
  watermark_text?: string
  link_buttons?: Array<{ text: string; url: string }>
  affiliate_kit?: { enabled: boolean; prefix?: string; replace_urls?: boolean }
  ocr_image_ai?: boolean
  smart_ai_rewrite?: boolean
  news_digest?: boolean
  ai_creativity?: number
  ai_max_tokens?: number
  paid_addons?: Record<string, boolean>
  metadata?: Record<string, any>
}

// AI Rewrite prompt templates (shared bot + dashboard)
export interface PromptTemplate {
  id: string
  name: string
  description?: string
  system_prompt: string
  user_prompt_template?: string
  target_language?: string
  current_version: number
  is_system?: boolean
  created_at?: number
  updated_at?: number
}

export interface SavePromptTemplateRequest {
  id?: string
  name: string
  description?: string
  system_prompt: string
  user_prompt_template?: string
  target_language?: string
}

export interface TestPromptRequest {
  prompt_id: string
  ai_config_id: string
  sample_text: string
  version?: number
}

export interface TestPromptResponse {
  success: boolean
  transformed_text: string
  error_message: string
  error_code: string
  execution_duration_ms: number
  provider_used: string
  model_used: string
  prompt_version_used: number
  circuit_state: string
}

export interface RoutePathQuickSetRequest {
  path_type: 'route1_direct' | 'route2_vip_hop' | 'route3_native'
  intermediate_channel_id?: string
  intermediate_channel_name?: string
  match_mode?: 'ALL' | 'ANY'
  keywords?: string[]
  regex_pattern?: string
  custom_header?: string
}

export interface Session {
  id: string
  phone_number: string
  user_id: string
  username: string
  first_name: string
  is_active: boolean
  is_authorized: boolean
  proxy?: string
  created_at: number
  updated_at: number
}

export interface AIConfig {
  id: string
  name: string
  provider: string
  model: string
  api_key_masked: string
  base_url: string
  system_prompt: string
  user_prompt_template: string
  temperature: number
  is_enabled: boolean
  target_language: string
}

export interface SaveAIConfigRequest {
  id?: string
  name: string
  provider: string
  model: string
  api_key?: string
  base_url?: string
  system_prompt: string
  user_prompt_template?: string
  temperature?: number
  is_enabled?: boolean
  target_language?: string
}

export interface FilterRule {
  id: string
  name: string
  whitelist_keywords: string[]
  blacklist_keywords: string[]
  regex_patterns: string[]
  allowed_media_types: string[]
  blocked_media_types: string[]
  drop_service_messages: boolean
  min_message_length: number
  max_message_length: number
}

export interface SaveFilterRuleRequest {
  id?: string
  name: string
  whitelist_keywords?: string[]
  blacklist_keywords?: string[]
  regex_patterns?: string[]
  allowed_media_types?: string[]
  blocked_media_types?: string[]
  drop_service_messages?: boolean
  min_message_length?: number
  max_message_length?: number
}

export interface DeliveryJob {
  id: string
  rule_id: string
  source_chat_id: string
  source_message_id: number
  target_chat_id: string
  status: string
  attempts: number
  max_attempts: number
  error_detail?: string | null
  delivery_stage: string
  created_at: number
}

export interface DeadLetterJob {
  id: string
  job_id?: string | null
  rule_id: string
  source_chat_id: string
  source_message_id: number
  target_chat_id: string
  attempts: number
  last_error: string
  error_category: string
  dead_lettered_at: number
}

export interface RuleLiveStat {
  rule_id: string
  rule_name: string
  is_active: boolean
  is_paused: boolean
  forwarded: number
  filtered: number
  errors: number
  last_forward_ts: number
  last_error: string
}

export interface RecentError {
  ts: number
  rule_id: string
  category: string
  error_name: string
  severity: string
  detail: string
  chat_id: string
}

export interface DeliveryStats {
  processed_total: number
  forwarded_total: number
  failed_total: number
  dedup_skipped_total: number
  filtered_total: number
  in_queue: number
  dead_lettered_total: number
  retry_total: number
  rules: RuleLiveStat[]
  errors: RecentError[]
}

export interface GatewaySystemInfo {
  atf_core_online: boolean
  atf_logger_online: boolean
  atf_web_online: boolean
  uptime_seconds: number
  grpc_port: number
  logger_port: number
  web_port: number
  version: string
  db_size_bytes: number
  total_rules: number
  total_sessions: number
}

export interface ServiceHealth {
  name: string
  online: boolean
  detail: string
  latency_ms: number
}

export interface ServicesHealthResponse {
  status: 'healthy' | 'degraded'
  time: string
  services: ServiceHealth[]
}

/**
 * One line of the server's configuration contract.
 *
 * `set` is a boolean by construction: /api/system/env reports *whether* a key is
 * configured and never its value, so a dashboard can display the contract
 * without a token ever reaching the browser.
 */
export interface EnvCheck {
  key: string
  level: 'required' | 'recommended' | 'optional'
  set: boolean
  reason: string
  hint: string
}

export interface EnvContract {
  ok: boolean
  deployment: string
  checks: EnvCheck[]
  missing_required: string[]
  missing_recommended: string[]
}

/** GET /api/system/setup — readiness verdict + evidence, shared by CLI, web, mobile. */
export interface SetupReport {
  ready: boolean
  time: string
  deployment: string
  product_version: string
  api_version: string
  commit_sha: string
  features: string[]
  checks: EnvCheck[]
  services: ServiceHealth[]
  missing_required: string[]
  missing_recommended: string[]
  next_steps: string[]
}

export interface SimulateRequest {
  rule_id?: string
  test_text: string
  forward_origin_chat_id?: string
  forward_origin_title?: string
  forward_origin_username?: string
  has_media?: boolean
  media_type?: string
  is_protected?: boolean
  rule_override?: SaveRuleRequest
}

export interface SimulateResponse {
  matched: boolean
  is_vip: boolean
  detected_category: string
  decision: string
  route_label_fa: string
  route_label_en: string
  reason: string
  conditions_summary: string[]
  action_steps: string[]
  preview_message: string
  protected_content_handled: boolean
}

export interface StatsResponse {
  total_rules: number
  active_rules: number
  total_sessions: number
  active_sessions: number
  total_forwarded_24h: number
  total_errors_24h: number
  queue_jobs_pending: number
  queue_jobs_failed: number
  atf_core_online: boolean
  atf_logger_online: boolean
}

export interface LogItem {
  id?: number | string
  ts: number
  service?: string
  category: string
  level?: string
  severity?: string
  error_name?: string
  message?: string
  detail: string
  /** v18: the task this event belongs to (empty for system-wide events). */
  rule_id?: string
  /** v18: the tenant this event belongs to (0 = system-wide). */
  user_id?: number
  /** v18: ties this record to its API request / audit event. */
  correlation_id?: string
  chat_id?: string
}

/** v18: a page of history plus the real match count and depth metadata. */
export interface LogPage {
  logs: LogItem[]
  /** Every row matching the filter, not just this page. */
  total: number
  /** Rows in this page. */
  count: number
  offset: number
  limit: number
  has_more: boolean
  rule_id?: string
}

export interface LogQuery {
  level?: string
  /** Multi-select severities (takes precedence over `level`). */
  levels?: string[]
  service?: string
  category?: string
  search?: string
  rule_id?: string
  correlation_id?: string
  user_id?: number
  /** Scope to the caller's own tenant. */
  mine?: boolean
  since?: number
  until?: number
  /** "The last N hours" shorthand for `since`. */
  hours?: number
  offset?: number
  page?: number
  limit?: number
  ascending?: boolean
}

export interface LogStats {
  total: number
  by_level: Record<string, number>
  by_category: Record<string, number>
  /** v18: which task produced how much history. */
  by_rule?: Record<string, number>
  /** v18: which service produced how much history. */
  by_service?: Record<string, number>
  /** v18: how far back the archive actually goes. */
  oldest_ts?: number
  newest_ts?: number
}

export interface PurgeLogsRequest {
  level?: string
  service?: string
  category?: string
  /** v18: purge one task's history only (ownership checked server-side). */
  rule_id?: string
  /** Keep logs newer than this many hours. 0 = no protection. */
  older_than_hours?: number
}

export interface PurgeLogsResponse {
  success: boolean
  purged: number
  remaining: number
  error?: string
}

// ---- Collection policy (per producer) -------------------------------------
//
// The policy is enforced per process, so every reading is scoped and labelled.
// A scope that cannot be read is reported as unavailable *with the reason* rather
// than as zeros: a zero would read as "nothing was suppressed", which is the
// opposite of the truth.

export interface LogPolicyCounters {
  emitted: number
  emitted_security: number
  suppressed_level: number
  suppressed_rate: number
  diagnostic_activations: number
}

export interface LogPolicyScope {
  available: boolean
  scope: string
  process?: string
  reason?: string
  configured_levels?: string[]
  effective_levels?: string[]
  diagnostic_active: boolean
  diagnostic_until?: number
  diagnostic_seconds_remaining?: number
  verbose_rate_per_second?: number
  counters?: LogPolicyCounters
  counter_scope?: string
  observed_at: string
}

export type LogPolicyScopes = Record<string, LogPolicyScope>

export interface LogPolicyResponse {
  generated_at: string
  scopes: LogPolicyScopes
  notes?: string[]
}

/** A change to the core's policy. Omitted fields are left as they are. */
export interface LogPolicyUpdateRequest {
  levels?: string[]
  verbose_rate_per_second?: number
  /** > 0 opens a bounded window; 0 leaves an active one alone. */
  diagnostic_seconds?: number
  disable_diagnostics?: boolean
}

export interface LogPolicyUpdateResponse {
  ok: boolean
  applied: boolean
  /** false = in force now but reset by a restart. */
  persisted: boolean
  before: Record<string, unknown>
  /** The policy read back from the core process after the change. */
  scope: LogPolicyScope
  notes?: string[]
}

// ---- Centralized audit events (observability platform) --------------------

export interface AuditEventEntry {
  event_id: string
  schema_version?: string
  event_type: string
  event_category?: string
  severity?: string
  outcome?: string
  ts_utc: number
  service?: string
  correlation_id?: string
  operation_id?: string
  tenant_id?: string
  actor_type?: string
  actor_id?: string
  feature_id?: string
  action?: string
  resource_type?: string
  resource_id?: string
  rule_id?: string
  queue_job_id?: string
  reason_code?: string
  duration_ms?: number
  error_code?: string
  error_class?: string
  retryable?: boolean
  result_summary?: string
  metadata_json?: string
}

export interface QueryAuditEventsParams {
  correlation_id?: string
  operation_id?: string
  feature_id?: string
  service?: string
  severity?: string
  event_category?: string
  outcome?: string
  since?: number
  until?: number
  limit?: number
  offset?: number
  /** Admin-only cross-tenant query; the backend pins non-admins to their own id. */
  owner_user_id?: number
}

export interface QueryAuditEventsResponse {
  events: AuditEventEntry[]
  total: number
}

export interface WebAccount {
  user_id: number;
  username: string;
  display_name: string;
  is_admin: boolean;
  plan: string;
  created_at: number;
}

export interface RegisterAccountRequest {
  user_id?: number;
  username?: string;
  password?: string;
  display_name?: string;
  is_admin?: boolean;
  plan?: string;
}

export interface RegisterAccountResponse {
  success: boolean;
  message: string;
  error_code?: string;
  account?: WebAccount;
}

export interface LoginAccountRequest {
  username?: string;
  password?: string;
  telegram_user_id?: number;
}

export interface LoginAccountResponse {
  success: boolean;
  message: string;
  error_code?: string;
  token?: string;
  expires_at?: number;
  account?: WebAccount;
}

export interface ValidateTokenRequest {
  token: string;
}

export interface ValidateTokenResponse {
  valid: boolean;
  user_id?: number;
  username?: string;
  is_admin?: boolean;
  error?: string;
}

export interface IssueWebTokenRequest {
  user_id: number;
}

export interface ChangePasswordRequest {
  user_id: number;
  old_password?: string;
  new_password: string;
}

export interface GetAccountRequest {
  user_id?: number;
  username?: string;
}

// Type alias for frontend usage
export type AuthResponse = RegisterAccountResponse | LoginAccountResponse | ValidateTokenResponse;

export interface PVResponderConfig {
  enabled: boolean;
  ai_config_id?: string;
  persona_prompt: string;
  typing_delay_min: number;
  typing_delay_max: number;
  cooldown_seconds: number;
  ignore_bots: boolean;
  history_limit: number;
}

// ---------------------------------------------------------------- Backup/Restore
// Whole-installation migration between servers. The bundle carries LIVE
// CREDENTIALS, so an optional passphrase encrypts it with AES-GCM.
export interface BackupExportRequest {
  /** 0 = every tenant (admin only). Non-admins always get just their own. */
  owner_user_id?: number
  /** When set, the bundle is encrypted with AES-GCM before it is returned. */
  passphrase?: string
}

export interface BackupExportResponse {
  success: boolean
  /** JSON snapshot, plain or `enc:v1:`-prefixed when a passphrase was given. */
  bundle: string
  /** entity name -> row count, so the UI can preview what is inside. */
  counts: Record<string, number>
  /** True when the bundle is actually encrypted (for the UI badge). */
  encrypted: boolean
  error?: string
}

export interface BackupImportRequest {
  bundle: string
  /** Required when the bundle is `enc:v1:`-prefixed. */
  passphrase?: string
  /** Re-stamp every imported entity with this owner. 0 = keep source ids. */
  target_owner_user_id?: number
  /** Replace rows whose id already exists here. Default false = skip. */
  overwrite_existing?: boolean
}

export interface EntityImportStats {
  imported: number
  skipped: number
  failed: number
}

export interface BackupImportResponse {
  success: boolean
  stats: Record<string, EntityImportStats>
  error?: string
}

// --------------------------------------------------------------- Diagnostics
// Mirrors GET /api/diagnostics — the preflight "catch problems early" report.
export type DiagnosticSeverity = 'error' | 'warning' | 'info'

export interface DiagnosticFinding {
  id: string
  severity: DiagnosticSeverity
  scope: 'service' | 'session' | 'ai' | 'rule'
  scope_id?: string
  title: string
  detail: string
  remediation: string
}

export interface DiagnosticsResponse {
  status: 'healthy' | 'warning' | 'critical'
  score: number
  generated_at: string
  counts: Record<DiagnosticSeverity, number>
  services: Array<{ name: string; online: boolean; detail?: string }>
  scanned: { rules: number; sessions: number; ai_configs: number }
  findings: DiagnosticFinding[]
}


// ---------------------------------------------------------------------------
// Per-task settings (server-driven schema)
// ---------------------------------------------------------------------------

/** One controllable per-task setting, as described by the server. */
export interface RuleSettingSpec {
  key: string
  kind: 'bool' | 'int' | 'float' | 'str' | 'list' | 'dict'
  default: any
  group: string
  label: string
  help?: string
  choices?: string[]
  minimum?: number
  maximum?: number
  nullable?: boolean
  item_kind?: string
}

export interface RuleSettingsSchema {
  version: number
  count: number
  groups: string[]
  settings: RuleSettingSpec[]
}

export interface RuleSettingsResponse {
  success: boolean
  message?: string
  error_code?: string
  settings?: Record<string, any>
  schema?: RuleSettingsSchema
  requested?: number
}
