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
  custom_header: string
  custom_footer: string
  split_long_caption: boolean
  filter_rule_id?: string | null
  ai_config_id?: string | null
  remove_links?: boolean
  delay_seconds?: number
  rate_limit_per_minute?: number
  max_retries?: number
  replacements?: Record<string, string>
  domain_allowlist?: string[]
  domain_blocklist?: string[]
  link_rewrite_map?: Record<string, string>
  created_at: number
  updated_at: number
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
  remove_links?: boolean
  delay_seconds?: number
  rate_limit_per_minute?: number
  max_retries?: number
  replacements?: Record<string, string>
  domain_allowlist?: string[]
  domain_blocklist?: string[]
  link_rewrite_map?: Record<string, string>
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
  id: number
  ts: number
  category: string
  error_name: string
  severity: string
  detail: string
  rule_id?: string
  chat_id?: string
}
