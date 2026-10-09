mod db;
mod engine;
mod models;

use axum::{
    extract::{Path, State},
    http::{Method, StatusCode},
    response::{Html, IntoResponse, Json},
    routing::{delete, get, post},
    Router,
};
use std::net::SocketAddr;
use std::path::{Path as StdPath, PathBuf};
use std::sync::Arc;
use tower_http::cors::{Any, CorsLayer};
use tower_http::services::ServeDir;
use tracing::info;

use crate::db::DbManager;
use crate::engine::SmartSimulator;
use crate::models::*;

#[derive(Clone)]
struct AppState {
    db: DbManager,
    frontend_dist: PathBuf,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "atf_web_backend=info,tower_http=info".into()),
        )
        .init();

    info!("Starting AutoTelegramForward Unified Gateway Web Service (Rust + Axum)...");

    let db_path = std::env::var("ATF_DB_PATH").unwrap_or_else(|_| "data/atf.db".to_string());
    let logs_db_path = std::env::var("ATF_LOGS_DB_PATH").unwrap_or_else(|_| "data/atf_logs.db".to_string());

    // Resolve db path relative to project root if running from web/backend
    let resolved_db = if StdPath::new(&db_path).exists() {
        db_path
    } else if StdPath::new("../../data/atf.db").exists() {
        "../../data/atf.db".to_string()
    } else {
        db_path
    };

    let resolved_logs = if StdPath::new(&logs_db_path).exists() {
        logs_db_path
    } else if StdPath::new("../../data/atf_logs.db").exists() {
        "../../data/atf_logs.db".to_string()
    } else {
        logs_db_path
    };

    info!("Connecting to SQLite DB at: {}", resolved_db);
    let db = DbManager::new(&resolved_db, &resolved_logs)
        .expect("Failed to initialize SQLite database connection");

    // Locate frontend dist
    let dist_candidates = vec![
        PathBuf::from("web/frontend/dist"),
        PathBuf::from("../frontend/dist"),
        PathBuf::from("./frontend/dist"),
        PathBuf::from("./dist"),
    ];
    let frontend_dist = dist_candidates
        .into_iter()
        .find(|p| p.join("index.html").exists())
        .unwrap_or_else(|| PathBuf::from("web/frontend/dist"));

    info!("Frontend static assets path: {:?}", frontend_dist);

    let state = Arc::new(AppState {
        db,
        frontend_dist: frontend_dist.clone(),
    });

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods([Method::GET, Method::POST, Method::PUT, Method::DELETE, Method::OPTIONS])
        .allow_headers(Any);

    let api_routes = Router::new()
        .route("/health", get(health_handler))
        .route("/gateway", get(gateway_info_handler))
        .route("/stats", get(stats_handler))
        .route("/rules", get(list_rules_handler).post(save_rule_handler))
        .route("/rules/:id", get(get_rule_handler).delete(delete_rule_handler))
        .route("/rules/:id/toggle", post(toggle_rule_handler))
        .route("/rules/:id/route-path", post(route_path_handler))
        .route("/sessions", get(list_sessions_handler))
        .route("/ai-configs", get(list_ai_configs_handler).post(save_ai_config_handler))
        .route("/ai-configs/:id", delete(delete_ai_config_handler))
        .route("/filters", get(list_filters_handler).post(save_filter_handler))
        .route("/filters/:id", delete(delete_filter_handler))
        .route("/queue", get(queue_jobs_handler))
        .route("/dlq", get(dlq_jobs_handler).delete(purge_dlq_handler))
        .route("/dlq/:id/retry", post(retry_dlq_handler))
        .route("/simulate", post(simulate_handler))
        .route("/logs", get(logs_handler))
        .with_state(state.clone());

    let mut app = Router::new()
        .nest("/api", api_routes)
        .layer(cors);

    // If dist exists, serve frontend SPA files
    if frontend_dist.join("index.html").exists() {
        let serve_dir = ServeDir::new(&frontend_dist);
        let fallback_state = state.clone();
        app = app.fallback_service(serve_dir).route(
            "/",
            get(move || {
                let p = fallback_state.frontend_dist.join("index.html");
                async move {
                    match tokio::fs::read_to_string(p).await {
                        Ok(content) => Html(content).into_response(),
                        Err(_) => (StatusCode::NOT_FOUND, "index.html not found").into_response(),
                    }
                }
            }),
        );
    }

    let bind_addr = std::env::var("ATF_WEB_ADDR").unwrap_or_else(|_| "0.0.0.0:8088".to_string());
    let addr: SocketAddr = bind_addr.parse().expect("Invalid ATF_WEB_ADDR format");

    info!("🚀 Web microservice gateway listening on http://{}", addr);
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn health_handler() -> impl IntoResponse {
    Json(serde_json::json!({
        "status": "healthy",
        "service": "atf-web-backend",
        "version": "1.2.0",
        "timestamp": chrono::Utc::now().timestamp(),
    }))
}

async fn gateway_info_handler(State(state): State<Arc<AppState>>) -> Result<Json<GatewaySystemInfo>, (StatusCode, String)> {
    state.db.get_gateway_info().map(Json).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))
}

async fn stats_handler(State(state): State<Arc<AppState>>) -> Result<Json<StatsResponse>, (StatusCode, String)> {
    state.db.get_stats().map(Json).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))
}

async fn list_rules_handler(State(state): State<Arc<AppState>>) -> Result<Json<Vec<ForwardRuleModel>>, (StatusCode, String)> {
    state.db.get_all_rules().map(Json).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))
}

async fn get_rule_handler(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> Result<Json<ForwardRuleModel>, (StatusCode, String)> {
    match state.db.get_rule(&id) {
        Ok(Some(r)) => Ok(Json(r)),
        Ok(None) => Err((StatusCode::NOT_FOUND, "Rule not found".to_string())),
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, e)),
    }
}

async fn save_rule_handler(
    State(state): State<Arc<AppState>>,
    Json(payload): Json<SaveRuleRequest>,
) -> Result<Json<ForwardRuleModel>, (StatusCode, String)> {
    state.db.save_rule(&payload).map(Json).map_err(|e| (StatusCode::BAD_REQUEST, e))
}

async fn delete_rule_handler(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    match state.db.delete_rule(&id) {
        Ok(true) => Ok(Json(serde_json::json!({ "success": true, "id": id }))),
        Ok(false) => Err((StatusCode::NOT_FOUND, "Rule not found".to_string())),
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, e)),
    }
}

async fn toggle_rule_handler(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> Result<Json<ForwardRuleModel>, (StatusCode, String)> {
    state.db.toggle_rule(&id).map(Json).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))
}

async fn route_path_handler(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
    Json(payload): Json<RoutePathQuickSetRequest>,
) -> Result<Json<ForwardRuleModel>, (StatusCode, String)> {
    state.db.quick_set_route_path(&id, &payload).map(Json).map_err(|e| (StatusCode::BAD_REQUEST, e))
}

async fn list_sessions_handler(State(state): State<Arc<AppState>>) -> Result<Json<Vec<SessionModel>>, (StatusCode, String)> {
    state.db.get_sessions().map(Json).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))
}

// AI Config Handlers
async fn list_ai_configs_handler(State(state): State<Arc<AppState>>) -> Result<Json<Vec<AIConfigModel>>, (StatusCode, String)> {
    state.db.get_all_ai_configs().map(Json).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))
}

async fn save_ai_config_handler(
    State(state): State<Arc<AppState>>,
    Json(payload): Json<SaveAIConfigRequest>,
) -> Result<Json<AIConfigModel>, (StatusCode, String)> {
    state.db.save_ai_config(&payload).map(Json).map_err(|e| (StatusCode::BAD_REQUEST, e))
}

async fn delete_ai_config_handler(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    match state.db.delete_ai_config(&id) {
        Ok(true) => Ok(Json(serde_json::json!({ "success": true, "id": id }))),
        Ok(false) => Err((StatusCode::NOT_FOUND, "AI Config not found".to_string())),
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, e)),
    }
}

// Filter Rule Handlers
async fn list_filters_handler(State(state): State<Arc<AppState>>) -> Result<Json<Vec<FilterRuleModel>>, (StatusCode, String)> {
    state.db.get_all_filter_rules().map(Json).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))
}

async fn save_filter_handler(
    State(state): State<Arc<AppState>>,
    Json(payload): Json<SaveFilterRuleRequest>,
) -> Result<Json<FilterRuleModel>, (StatusCode, String)> {
    state.db.save_filter_rule(&payload).map(Json).map_err(|e| (StatusCode::BAD_REQUEST, e))
}

async fn delete_filter_handler(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    match state.db.delete_filter_rule(&id) {
        Ok(true) => Ok(Json(serde_json::json!({ "success": true, "id": id }))),
        Ok(false) => Err((StatusCode::NOT_FOUND, "Filter Rule not found".to_string())),
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, e)),
    }
}

// Queue & DLQ Handlers
async fn queue_jobs_handler(State(state): State<Arc<AppState>>) -> Result<Json<Vec<DeliveryJobModel>>, (StatusCode, String)> {
    state.db.get_delivery_jobs(50).map(Json).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))
}

async fn dlq_jobs_handler(State(state): State<Arc<AppState>>) -> Result<Json<Vec<DeadLetterJobModel>>, (StatusCode, String)> {
    state.db.get_dead_letter_queue(50).map(Json).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))
}

async fn retry_dlq_handler(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    match state.db.retry_dead_letter_job(&id) {
        Ok(true) => Ok(Json(serde_json::json!({ "success": true, "retried_id": id }))),
        Ok(false) => Err((StatusCode::NOT_FOUND, "DLQ entry not found".to_string())),
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, e)),
    }
}

async fn purge_dlq_handler(State(state): State<Arc<AppState>>) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    match state.db.purge_dead_letter_queue() {
        Ok(count) => Ok(Json(serde_json::json!({ "success": true, "purged_count": count }))),
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, e)),
    }
}

async fn simulate_handler(
    State(state): State<Arc<AppState>>,
    Json(req): Json<SimulateRequest>,
) -> Result<Json<SimulateResponse>, (StatusCode, String)> {
    let rule = if let Some(ref r_override) = req.rule_override {
        ForwardRuleModel {
            id: r_override.id.clone().unwrap_or_else(|| "sim_rule".to_string()),
            session_id: "default".to_string(),
            source_chat_id: r_override.source_chat_id.clone(),
            source_chat_name: r_override.source_chat_name.clone().unwrap_or_default(),
            target_chat_id: r_override.target_chat_id.clone(),
            target_chat_name: r_override.target_chat_name.clone().unwrap_or_default(),
            routing_type: r_override.routing_type.clone().unwrap_or_else(|| "CHANNEL_TO_CHANNEL".to_string()),
            forward_mode: r_override.forward_mode.clone().unwrap_or_else(|| "COPY_MESSAGE".to_string()),
            is_active: r_override.is_active.unwrap_or(true),
            is_paused: r_override.is_paused.unwrap_or(false),
            priority: r_override.priority.unwrap_or(10),
            message_category: r_override.message_category.clone().unwrap_or_else(|| "ALL".to_string()),
            use_intermediate: r_override.use_intermediate.unwrap_or(false),
            intermediate_channel_id: r_override.intermediate_channel_id.clone().unwrap_or_default(),
            intermediate_channel_name: r_override.intermediate_channel_name.clone().unwrap_or_default(),
            fallback_mode: r_override.fallback_mode.clone().unwrap_or_else(|| "COPY_MESSAGE".to_string()),
            fallback_enabled: r_override.fallback_enabled.unwrap_or(true),
            detection_criteria: r_override.detection_criteria.clone().unwrap_or_else(|| serde_json::json!({})),
            link_policy: r_override.link_policy.clone().unwrap_or_else(|| "PRESERVE_ALL".to_string()),
            custom_header: r_override.custom_header.clone().unwrap_or_default(),
            custom_footer: r_override.custom_footer.clone().unwrap_or_default(),
            split_long_caption: r_override.split_long_caption.unwrap_or(true),
            filter_rule_id: r_override.filter_rule_id.clone(),
            ai_config_id: r_override.ai_config_id.clone(),
            remove_links: r_override.remove_links.unwrap_or(false),
            delay_seconds: r_override.delay_seconds.unwrap_or(0.0),
            rate_limit_per_minute: r_override.rate_limit_per_minute.unwrap_or(0),
            max_retries: r_override.max_retries.unwrap_or(3),
            replacements: r_override.replacements.clone().unwrap_or_else(|| serde_json::json!({})),
            domain_allowlist: r_override.domain_allowlist.clone().unwrap_or_default(),
            domain_blocklist: r_override.domain_blocklist.clone().unwrap_or_default(),
            link_rewrite_map: r_override.link_rewrite_map.clone().unwrap_or_else(|| serde_json::json!({})),
            created_at: 0,
            updated_at: 0,
        }
    } else if let Some(ref rule_id) = req.rule_id {
        state.db.get_rule(rule_id)
            .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))?
            .ok_or_else(|| (StatusCode::NOT_FOUND, "Rule not found".to_string()))?
    } else {
        // Use first active rule as default
        let rules = state.db.get_all_rules().map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))?;
        rules.into_iter().next().ok_or_else(|| (StatusCode::BAD_REQUEST, "No rules exist to simulate".to_string()))?
    };

    let sim_result = SmartSimulator::simulate(&rule, &req);
    Ok(Json(sim_result))
}

async fn logs_handler(State(state): State<Arc<AppState>>) -> Result<Json<Vec<LogItemModel>>, (StatusCode, String)> {
    state.db.get_recent_logs(50).map(Json).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))
}
