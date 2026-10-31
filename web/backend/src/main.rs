//! AutoTelegramForward Unified Web Gateway — entry point.
//!
//! Everything interesting lives in a module of its own:
//!
//! | module          | owns                                              |
//! |-----------------|---------------------------------------------------|
//! | `config`        | env parsing + validation (refuses to mis-start)   |
//! | `state`         | shared state, tuned upstream client, rate limiter |
//! | `proxy`         | `/api` streaming reverse proxy to the Go API      |
//! | `static_assets` | embedded SPA + compression + cache contract       |
//! | `security`      | CSP derivation + security headers                 |
//! | `rate_limit`    | per-client token bucket                           |
//! | `health`        | `/health`, `/healthz`, `/api/gateway`             |
//!
//! The external HTTP contract is unchanged from 2.0: same routes, same health
//! JSON, same embedded dashboard. What changed is how it survives production:
//! streaming bodies, hop-by-hop hygiene, idle-gap timeouts, graceful shutdown,
//! compression on the static surface, a rate-limited API, and a CSP that pins
//! the embedded HTML's actual inline-script hashes.

mod config;
mod health;
mod proxy;
mod rate_limit;
mod security;
mod state;
mod static_assets;

use axum::extract::State;
use axum::http::Method;

use axum::routing::{any, get};
use axum::Router;
use state::AppState;
use tower_http::cors::{Any, CorsLayer};
use tower_http::trace::TraceLayer;
use tracing::{error, info, warn};

fn init_tracing(json: bool) {
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| "atf_web_backend=info,tower_http=info".into());
    let subscriber = tracing_subscriber::fmt().with_env_filter(filter);
    if json {
        subscriber.json().init();
    } else {
        subscriber.init();
    }
}

/// SIGTERM (compose stop / k8s rolling update) or Ctrl+C stops accepting new
/// connections and lets in-flight requests finish instead of dropping them.
async fn shutdown_signal() {
    let ctrl_c = async {
        if tokio::signal::ctrl_c().await.is_err() {
            error!("failed to listen for Ctrl+C");
        }
    };
    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut sig) => {
                sig.recv().await;
            }
            Err(e) => error!("failed to install SIGTERM handler: {e}"),
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
    info!("shutdown signal received; draining in-flight requests");
}

fn allow_all_methods() -> [Method; 6] {
    [
        Method::GET,
        Method::POST,
        Method::PUT,
        Method::DELETE,
        Method::OPTIONS,
        Method::PATCH,
    ]
}

fn build_router(state: AppState) -> Router {
    // Same-origin by construction: the dashboard is served from this process,
    // so cross-origin API access is the exception, not the rule. An empty
    // allow-list keeps the old permissive behaviour (and says so loudly).
    let cors = if state.config.allowed_origins.is_empty() {
        warn!(
            "ATF_WEB_ALLOWED_ORIGINS is not set: /api answers any origin. \
             Set it (comma-separated) in production."
        );
        CorsLayer::new()
            .allow_origin(Any)
            .allow_methods(allow_all_methods())
            .allow_headers(Any)
    } else {
        let origins = state
            .config
            .allowed_origins
            .iter()
            .filter_map(|origin| origin.parse().ok())
            .collect::<Vec<_>>();
        CorsLayer::new()
            .allow_origin(origins)
            .allow_methods(allow_all_methods())
            .allow_headers(Any)
    };

    // Static surface: the SPA plus compression. The compression layer is
    // scoped to this nested router, not router-wide, so a proxied streaming
    // response is never routed through it.
    let static_router = Router::new()
        .fallback(get(|State(state): State<AppState>, uri: axum::http::Uri| async move {
            static_assets::serve(uri, &state.csp)
        }))
        .layer(static_assets::compression_layer())
        .with_state(state.clone());

    // `/api` (and only `/api`) proxies to the Control Plane; everything else
    // is the embedded SPA.
    Router::new()
        .route("/health", get(health::health_handler))
        .route("/healthz", get(health::health_handler))
        .route("/api/gateway", get(health::gateway_info_handler))
        .route("/api/*path", any(proxy::proxy_api_handler))
        .route("/api", any(proxy::proxy_api_handler))
        .fallback_service(static_router)
        .layer(cors)
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

fn main() {
    let config = match config::Config::from_env() {
        Ok(config) => config,
        Err(e) => {
            // Tracing may not be up yet; boot failures print plainly.
            eprintln!("atf-web-backend: invalid configuration, refusing to start: {e}");
            std::process::exit(2);
        }
    };
    init_tracing(config.log_format_json);

    // CSP: the operator's override, or one derived from the real embedded
    // index.html (pinning the hashes of its inline scripts).
    let csp = config.csp.clone().unwrap_or_else(|| {
        let html = static_assets::FrontendAssets::get("index.html")
            .map(|f| String::from_utf8_lossy(&f.data).to_string())
            .unwrap_or_default();
        security::default_csp(&html)
    });
    info!("Content-Security-Policy: {csp}");

    let state = state::build_state(config, csp);
    info!("Configured Upstream Control Plane (Go API): {}", state.config.upstream_api);
    info!(
        "Rate limit: {} rps / burst {} per client (ip header: {})",
        state.config.rate_limit_rps,
        state.config.rate_limit_burst,
        state.config.client_ip_header.as_deref().unwrap_or("<direct peer>")
    );

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("tokio runtime starts");
    runtime.block_on(async move {
        let router = build_router(state.clone());
        let listener = match tokio::net::TcpListener::bind(state.config.listen_addr).await {
            Ok(listener) => listener,
            Err(e) => {
                error!("cannot bind {}: {e}", state.config.listen_addr);
                std::process::exit(1);
            }
        };
        info!(
            "Web Gateway & Dashboard listening at http://{}",
            state.config.listen_addr
        );
        let svc = router.into_make_service_with_connect_info::<std::net::SocketAddr>();
        if let Err(e) = axum::serve(listener, svc)
            .with_graceful_shutdown(shutdown_signal())
            .await
        {
            error!("server error: {e}");
            std::process::exit(1);
        }
        info!("drained; goodbye");
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use axum::http::StatusCode;

    fn test_config(upstream: &str) -> Config {
        Config {
            listen_addr: "127.0.0.1:0".parse().unwrap(),
            upstream_api: upstream.to_string(),
            client_ip_header: None,
            rate_limit_rps: 1000.0,
            rate_limit_burst: 1000,
            max_body_bytes: 1024 * 1024,
            upstream_connect_timeout_secs: 2,
            upstream_read_timeout_secs: 5,
            allowed_origins: vec![],
            csp: None,
            log_format_json: false,
        }
    }

    /// A stub Control Plane that echoes what actually arrived, so assertions
    /// run on what the gateway really forwarded.
    async fn stub_upstream() -> (String, tokio::task::JoinHandle<()>) {
        let app = Router::new()
            .route(
                "/api/echo",
                any(|headers: axum::http::HeaderMap, body: axum::body::Body| async move {
                    use futures_util::StreamExt;
                    let connection = headers
                        .get("connection")
                        .map(|v| v.to_str().unwrap().to_string())
                        .unwrap_or_default();
                    let private = headers
                        .get("x-private")
                        .map(|v| v.to_str().unwrap().to_string())
                        .unwrap_or_default();
                    let mut bytes = Vec::new();
                    let mut stream = body.into_data_stream();
                    while let Some(chunk) = stream.next().await {
                        bytes.extend_from_slice(&chunk.unwrap());
                    }
                    axum::Json(serde_json::json!({
                        "connection_header": connection,
                        "private_header": private,
                        "body": String::from_utf8_lossy(&bytes),
                        "correlation": headers.get("x-correlation-id").map(|v| v.to_str().unwrap().to_string()).unwrap_or_default(),
                    }))
                }),
            )
            .route("/healthz", get(|| async { "ok" }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        (format!("http://{addr}"), handle)
    }

    #[tokio::test]
    async fn proxy_forwards_and_strips_hop_by_hop() {
        let (upstream, server) = stub_upstream().await;
        let state = state::build_state(test_config(&upstream), "default-src 'self'".into());
        let app = build_router(state);

        let request = axum::http::Request::builder()
            .method("POST")
            .uri("/api/echo")
            .header("content-type", "application/json")
            .header("connection", "keep-alive, X-Private")
            .header("x-private", "should-not-travel")
            .header("content-length", "18")
            .body(axum::body::Body::from("{\"ping\": \"pong\"}"))
            .unwrap();

        let response = tower::ServiceExt::oneshot(app, request).await.unwrap();
        let status = response.status();
        let body = http_body_util::BodyExt::collect(response.into_body())
            .await
            .unwrap()
            .to_bytes();
        eprintln!("proxy response status {status}: {}", String::from_utf8_lossy(&body));
        assert_eq!(status, 200);
        let payload: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(
            payload["body"], "{\"ping\": \"pong\"}",
            "request body must stream through intact"
        );
        assert_eq!(
            payload["connection_header"], "",
            "hop-by-hop Connection must not reach upstream"
        );
        assert_eq!(
            payload["private_header"], "",
            "Connection-named headers must not reach upstream"
        );
        assert!(
            !payload["correlation"].as_str().unwrap_or("").is_empty(),
            "gateway must attach a correlation id"
        );
        server.abort();
    }

    #[tokio::test]
    async fn proxy_returns_502_when_upstream_is_down() {
        // Nothing listens on the reserved documentation port.
        let state = state::build_state(test_config("http://127.0.0.1:1"), "default-src 'self'".into());
        let app = build_router(state);
        let request = axum::http::Request::builder()
            .method("GET")
            .uri("/api/anything")
            .body(axum::body::Body::empty())
            .unwrap();
        let response = tower::ServiceExt::oneshot(app, request).await.unwrap();
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    }

    #[tokio::test]
    async fn oversized_content_length_is_refused_early() {
        let (upstream, server) = stub_upstream().await;
        let mut cfg = test_config(&upstream);
        cfg.max_body_bytes = 8;
        let state = state::build_state(cfg, "default-src 'self'".into());
        let app = build_router(state);
        let request = axum::http::Request::builder()
            .method("POST")
            .uri("/api/echo")
            .header("content-length", "100")
            .body(axum::body::Body::from("x".repeat(100)))
            .unwrap();
        let response = tower::ServiceExt::oneshot(app, request).await.unwrap();
        assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
        server.abort();
    }

    #[tokio::test]
    async fn rate_limited_client_gets_429_with_retry_after() {
        let (upstream, server) = stub_upstream().await;
        let mut cfg = test_config(&upstream);
        cfg.rate_limit_rps = 0.0001;
        cfg.rate_limit_burst = 1;
        cfg.client_ip_header = Some("X-Forwarded-For".to_string());
        let state = state::build_state(cfg, "default-src 'self'".into());
        let app = build_router(state);

        let make = || {
            axum::http::Request::builder()
                .method("GET")
                .uri("/api/nope")
                .header("x-forwarded-for", "203.0.113.7")
                .body(axum::body::Body::empty())
                .unwrap()
        };
        // First request consumes the single burst token (its status is the
        // upstream's business), the second must be rate-limited locally.
        let first = tower::ServiceExt::oneshot(app.clone(), make()).await.unwrap();
        let _ = first;
        let second = tower::ServiceExt::oneshot(app, make()).await.unwrap();
        assert_eq!(second.status(), StatusCode::TOO_MANY_REQUESTS);
        assert!(second.headers().get("retry-after").is_some());
        server.abort();
    }

    #[tokio::test]
    async fn static_surface_serves_index_with_security_headers() {
        // The embedded assets exist after `npm run build` (same precondition
        // as the historical embedded-assets test).
        if static_assets::FrontendAssets::get("index.html").is_none() {
            eprintln!("skipping: frontend dist not built");
            return;
        }
        let (upstream, server) = stub_upstream().await;
        let state = state::build_state(test_config(&upstream), "default-src 'self'".into());
        let app = build_router(state);
        let request = axum::http::Request::builder()
            .method("GET")
            .uri("/")
            .body(axum::body::Body::empty())
            .unwrap();
        let response = tower::ServiceExt::oneshot(app, request).await.unwrap();
        assert_eq!(response.status(), 200);
        let headers = response.headers();
        assert!(headers.get("content-security-policy").is_some());
        assert_eq!(headers.get("x-content-type-options").unwrap(), "nosniff");
        assert_eq!(
            headers.get("cache-control").unwrap(),
            "no-cache, no-store, must-revalidate"
        );
        assert!(
            headers.get("x-frame-options").is_none(),
            "index.html stays frameable for TMA"
        );
        server.abort();
    }
}
