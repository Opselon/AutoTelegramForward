//! Health and gateway-info endpoints.
//!
//! The JSON shapes are load-bearing: the docker-compose healthcheck reads
//! `status`, the dashboard reads `version` + `upstream_connected`, and the
//! version previously disagreed with the crate version (1.2.2 vs 2.0.0), which
//! made `docker inspect --format health` and the dashboard disagree about
//! which gateway was running. `env!("CARGO_PKG_VERSION")` keeps one source of
//! truth; do not inline a literal.

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;
use std::time::Duration;

use crate::state::AppState;

async fn upstream_connected(state: &AppState) -> bool {
    let url = format!("{}/healthz", state.config.upstream_api);
    match state
        .client
        .get(&url)
        .timeout(Duration::from_secs(3))
        .send()
        .await
    {
        Ok(resp) => resp.status().is_success(),
        Err(_) => false,
    }
}

/// `GET /health` and `GET /healthz`: the gateway's own verdict plus whether
/// the Control Plane answered its probe.
pub async fn health_handler(State(state): State<AppState>) -> Response {
    let upstream_ok = upstream_connected(&state).await;
    (
        StatusCode::OK,
        Json(json!({
            "service": "atf-web-gateway",
            "version": env!("CARGO_PKG_VERSION"),
            "status": "healthy",
            "upstream_api": state.config.upstream_api,
            "upstream_connected": upstream_ok
        })),
    )
        .into_response()
}

/// `GET /api/gateway`: what the dashboard header consumes (version, role,
/// feature flags). Response shape is part of the API contract with the UI.
pub async fn gateway_info_handler(State(state): State<AppState>) -> Response {
    Json(json!({
        "service": "atf-web-gateway",
        "version": env!("CARGO_PKG_VERSION"),
        "status": "healthy",
        "role": "unified_web_gateway_bff",
        "upstream_api": state.config.upstream_api,
        "features": {
            "embedded_react_dashboard": true,
            "reverse_proxy_control_plane": true,
            "security_headers": true,
            "request_streaming": true,
            "rate_limiting": true,
            "compression": true
        }
    }))
    .into_response()
}
