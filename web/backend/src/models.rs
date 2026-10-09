use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ForwardRuleModel {
    pub id: String,
    pub session_id: String,
    pub source_chat_id: String,
    pub source_chat_name: String,
    pub target_chat_id: String,
    pub target_chat_name: String,
    pub routing_type: String,
    pub forward_mode: String,
    pub is_active: bool,
    pub is_paused: bool,
    pub priority: i32,
    pub message_category: String,
    pub use_intermediate: bool,
    pub intermediate_channel_id: String,
    pub intermediate_channel_name: String,
    pub fallback_mode: String,
    pub fallback_enabled: bool,
    pub detection_criteria: serde_json::Value,
    pub link_policy: String,
    pub custom_header: String,
    pub custom_footer: String,
    pub split_long_caption: bool,
    pub filter_rule_id: Option<String>,
    pub ai_config_id: Option<String>,
    pub remove_links: bool,
    pub delay_seconds: f64,
    pub rate_limit_per_minute: i32,
    pub max_retries: i32,
    pub replacements: serde_json::Value,
    pub domain_allowlist: Vec<String>,
    pub domain_blocklist: Vec<String>,
    pub link_rewrite_map: serde_json::Value,
    pub created_at: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SaveRuleRequest {
    pub id: Option<String>,
    pub session_id: Option<String>,
    pub source_chat_id: String,
    pub source_chat_name: Option<String>,
    pub target_chat_id: String,
    pub target_chat_name: Option<String>,
    pub routing_type: Option<String>,
    pub forward_mode: Option<String>,
    pub is_active: Option<bool>,
    pub is_paused: Option<bool>,
    pub priority: Option<i32>,
    pub message_category: Option<String>,
    pub use_intermediate: Option<bool>,
    pub intermediate_channel_id: Option<String>,
    pub intermediate_channel_name: Option<String>,
    pub fallback_mode: Option<String>,
    pub fallback_enabled: Option<bool>,
    pub detection_criteria: Option<serde_json::Value>,
    pub link_policy: Option<String>,
    pub custom_header: Option<String>,
    pub custom_footer: Option<String>,
    pub split_long_caption: Option<bool>,
    pub filter_rule_id: Option<String>,
    pub ai_config_id: Option<String>,
    pub remove_links: Option<bool>,
    pub delay_seconds: Option<f64>,
    pub rate_limit_per_minute: Option<i32>,
    pub max_retries: Option<i32>,
    pub replacements: Option<serde_json::Value>,
    pub domain_allowlist: Option<Vec<String>>,
    pub domain_blocklist: Option<Vec<String>>,
    pub link_rewrite_map: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RoutePathQuickSetRequest {
    pub path_type: String, // "route1_direct" | "route2_vip_hop" | "route3_native"
    pub intermediate_channel_id: Option<String>,
    pub intermediate_channel_name: Option<String>,
    pub match_mode: Option<String>, // "ALL" | "ANY"
    pub keywords: Option<Vec<String>>,
    pub regex_pattern: Option<String>,
    pub custom_header: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionModel {
    pub id: String,
    pub phone_number: String,
    pub user_id: String,
    pub username: String,
    pub first_name: String,
    pub is_active: bool,
    pub is_authorized: bool,
    pub proxy: String,
    pub created_at: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AIConfigModel {
    pub id: String,
    pub name: String,
    pub provider: String,
    pub model: String,
    pub api_key_masked: String,
    pub base_url: String,
    pub system_prompt: String,
    pub user_prompt_template: String,
    pub temperature: f64,
    pub is_enabled: bool,
    pub target_language: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SaveAIConfigRequest {
    pub id: Option<String>,
    pub name: String,
    pub provider: String,
    pub model: String,
    pub api_key: Option<String>,
    pub base_url: Option<String>,
    pub system_prompt: String,
    pub user_prompt_template: Option<String>,
    pub temperature: Option<f64>,
    pub is_enabled: Option<bool>,
    pub target_language: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TestAIRequest {
    pub config_id: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub system_prompt: Option<String>,
    pub temperature: Option<f64>,
    pub sample_text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TestAIResponse {
    pub success: bool,
    pub output_text: String,
    pub latency_ms: u64,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FilterRuleModel {
    pub id: String,
    pub name: String,
    pub whitelist_keywords: Vec<String>,
    pub blacklist_keywords: Vec<String>,
    pub regex_patterns: Vec<String>,
    pub allowed_media_types: Vec<String>,
    pub blocked_media_types: Vec<String>,
    pub drop_service_messages: bool,
    pub min_message_length: i64,
    pub max_message_length: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SaveFilterRuleRequest {
    pub id: Option<String>,
    pub name: String,
    pub whitelist_keywords: Option<Vec<String>>,
    pub blacklist_keywords: Option<Vec<String>>,
    pub regex_patterns: Option<Vec<String>>,
    pub allowed_media_types: Option<Vec<String>>,
    pub blocked_media_types: Option<Vec<String>>,
    pub drop_service_messages: Option<bool>,
    pub min_message_length: Option<i64>,
    pub max_message_length: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeliveryJobModel {
    pub id: String,
    pub rule_id: String,
    pub source_chat_id: String,
    pub source_message_id: i64,
    pub target_chat_id: String,
    pub status: String,
    pub attempts: i32,
    pub max_attempts: i32,
    pub error_detail: Option<String>,
    pub delivery_stage: String,
    pub created_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeadLetterJobModel {
    pub id: String,
    pub job_id: Option<String>,
    pub rule_id: String,
    pub source_chat_id: String,
    pub source_message_id: i64,
    pub target_chat_id: String,
    pub attempts: i32,
    pub last_error: String,
    pub error_category: String,
    pub dead_lettered_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GatewaySystemInfo {
    pub atf_core_online: bool,
    pub atf_logger_online: bool,
    pub atf_web_online: bool,
    pub uptime_seconds: u64,
    pub grpc_port: u16,
    pub logger_port: u16,
    pub web_port: u16,
    pub version: String,
    pub db_size_bytes: u64,
    pub total_rules: i64,
    pub total_sessions: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SimulateRequest {
    pub rule_id: Option<String>,
    pub test_text: String,
    pub forward_origin_chat_id: Option<String>,
    pub forward_origin_title: Option<String>,
    pub forward_origin_username: Option<String>,
    pub has_media: Option<bool>,
    pub media_type: Option<String>,
    pub is_protected: Option<bool>,
    pub rule_override: Option<SaveRuleRequest>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SimulateResponse {
    pub matched: bool,
    pub is_vip: bool,
    pub detected_category: String,
    pub decision: String,
    pub route_label_fa: String,
    pub route_label_en: String,
    pub reason: String,
    pub conditions_summary: Vec<String>,
    pub action_steps: Vec<String>,
    pub preview_message: String,
    pub protected_content_handled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StatsResponse {
    pub total_rules: i64,
    pub active_rules: i64,
    pub total_sessions: i64,
    pub active_sessions: i64,
    pub total_forwarded_24h: i64,
    pub total_errors_24h: i64,
    pub queue_jobs_pending: i64,
    pub queue_jobs_failed: i64,
    pub atf_core_online: bool,
    pub atf_logger_online: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogItemModel {
    pub id: i64,
    pub ts: i64,
    pub category: String,
    pub error_name: String,
    pub severity: String,
    pub detail: String,
    pub rule_id: Option<String>,
    pub chat_id: Option<String>,
}
