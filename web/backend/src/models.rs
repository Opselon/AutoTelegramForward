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
    pub created_at: i64,
    pub updated_at: i64,
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
    // Optional ad-hoc rule overrides for testing directly in simulator
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
