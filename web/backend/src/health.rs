//! Health and gateway-info endpoints.
//!
//! The JSON shapes are load-bearing: the docker-compose healthcheck reads the
//! status code, the dashboard reads `version` + `upstream_connected` (and the
//! service chips read `atf_web_online`), and the version previously disagreed
//! with the crate version (1.2.2 vs 2.0.0), which made
//! `docker inspect --format health` and the dashboard disagree about which
//! gateway was running. `env!("CARGO_PKG_VERSION")` keeps one source of
//! truth; do not inline a literal.
//!
//! The upstream's internal URL is deliberately not published: it names an
//! address that is only resolvable inside the deployment network, and nothing
//! in the stack consumes it. The `upstream_connected` boolean is the part
//! callers act on.

use std::time::Instant;

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;
use std::time::Duration;

use crate::state::{AppState, UPSTREAM_PROBE_TTL};

/// Probe the Control Plane's `/healthz`, reusing a verdict younger than
/// `UPSTREAM_PROBE_TTL` so a burst of health checks (docker healthcheck,
/// dashboard loads) does not each pay a fresh upstream round-trip.
async fn upstream_connected(state: &AppState) -> bool {
    {
        let cache = state.upstream_probe.read().await;
        if let Some((at, ok)) = *cache {
            if at.elapsed() < UPSTREAM_PROBE_TTL {
                return ok;
            }
        }
    }
    let url = format!("{}/healthz", state.config.upstream_api);
    let verdict = match state
        .client
        .get(&url)
        .timeout(Duration::from_secs(3))
        .send()
        .await
    {
        Ok(resp) => resp.status().is_success(),
        Err(_) => false,
    };
    // Store the fresh verdict; the last writer wins, which is fine for a
    // single-value cache.
    *state.upstream_probe.write().await = Some((Instant::now(), verdict));
    verdict
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
            "upstream_connected": upstream_ok
        })),
    )
        .into_response()
}

/// `GET /api/gateway`: what the dashboard header consumes (version, role,
/// feature flags, this gateway's own online flag). Response shape is part of
/// the API contract with the UI — `atf_web_online` used to be missing, which
/// left the dashboard's "Web:8088" chip permanently offline.
pub async fn gateway_info_handler(State(state): State<AppState>) -> Response {
    let upstream_ok = upstream_connected(&state).await;
    Json(json!({
        "service": "atf-web-gateway",
        "version": env!("CARGO_PKG_VERSION"),
        "status": "healthy",
        "role": "unified_web_gateway_bff",
        "atf_web_online": true,
        "upstream_connected": upstream_ok,
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use crate::state::build_state;
    use std::sync::Arc;

    fn config(upstream: &str) -> Config {
        Config {
            listen_addr: "127.0.0.1:0".parse().unwrap(),
            upstream_api: upstream.to_string(),
            client_ip_header: None,
            rate_limit_rps: 100.0,
            rate_limit_burst: 100,
            max_body_bytes: 1024 * 1024,
            upstream_connect_timeout_secs: 2,
            upstream_read_timeout_secs: 5,
            shutdown_grace_secs: 25,
            allowed_origins: vec![],
            csp: None,
            log_format_json: false,
        }
    }

    async fn stub_upstream_healthy() -> (String, tokio::task::JoinHandle<()>) {
        let app = axum::Router::new().route("/healthz", axum::routing::get(|| async { "ok" }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        (format!("http://{addr}"), handle)
    }

    #[tokio::test]
    async fn health_reports_upstream_state_without_the_internal_url() {
        let (upstream, server) = stub_upstream_healthy().await;
        let state = build_state(config(&upstream), "default-src 'self'".into()).unwrap();

        let resp = health_handler(State(state.clone())).await;
        assert_eq!(resp.status(), StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), usize::MAX)
            .await
            .unwrap();
        let payload: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(payload["status"], "healthy");
        assert_eq!(payload["upstream_connected"], true);
        assert!(
            payload.get("upstream_api").is_none(),
            "the internal upstream URL must not be published"
        );
        assert_eq!(payload["version"], env!("CARGO_PKG_VERSION"));
        server.abort();
    }

    #[tokio::test]
    async fn gateway_info_carries_atf_web_online() {
        let (upstream, server) = stub_upstream_healthy().await;
        let state = build_state(config(&upstream), "default-src 'self'".into()).unwrap();

        let resp = gateway_info_handler(State(state)).await;
        let body = axum::body::to_bytes(resp.into_body(), usize::MAX)
            .await
            .unwrap();
        let payload: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(
            payload["atf_web_online"], true,
            "the dashboard's web chip reads this flag"
        );
        assert_eq!(payload["upstream_connected"], true);
        assert!(payload.get("upstream_api").is_none());
        server.abort();
    }

    #[tokio::test]
    async fn upstream_probe_is_cached_within_the_ttl() {
        let (upstream, server) = stub_upstream_healthy().await;
        let state = build_state(config(&upstream), "default-src 'self'".into()).unwrap();

        assert!(
            upstream_connected(&state).await,
            "first probe hits the stub"
        );
        let cached = { *state.upstream_probe.read().await };
        assert!(cached.is_some(), "the verdict is stored");

        // Second call within the TTL must reuse the cache (even if the probe
        // URL were now dead, the cached true survives).
        server.abort();
        assert!(
            upstream_connected(&state).await,
            "a verdict younger than the TTL is reused"
        );
    }

    #[tokio::test]
    async fn unreachable_upstream_reports_false() {
        let state: Arc<crate::state::GatewayState> =
            build_state(config("http://127.0.0.1:1"), "default-src 'self'".into()).unwrap();
        assert!(!upstream_connected(&state).await);
        assert!(
            !upstream_connected(&state).await,
            "the false verdict is cached too"
        );
    }
}
