use axum::{
    extract::{Request, State},
    http::{header, Method, StatusCode, Uri},
    response::{Html, IntoResponse, Json, Response},
    routing::{any, get},
    Router,
};
use rust_embed::RustEmbed;
use serde_json::json;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;
use tower_http::cors::{Any, CorsLayer};
use tracing::{error, info, warn};

#[derive(RustEmbed)]
#[folder = "../frontend/dist"]
struct FrontendAssets;

#[derive(Clone)]
struct AppState {
    api_upstream: String,
    client: reqwest::Client,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "atf_web_backend=info,tower_http=info".into()),
        )
        .init();

    info!("Starting AutoTelegramForward Unified Web Gateway (Rust + Axum)...");

    let listen_addr = std::env::var("ATF_WEB_ADDR").unwrap_or_else(|_| "0.0.0.0:8088".to_string());
    let upstream_api = std::env::var("ATF_UPSTREAM_API")
        .or_else(|_| std::env::var("ATF_API_URL"))
        .unwrap_or_else(|_| "http://127.0.0.1:8080".to_string());

    info!("Configured Upstream Control Plane (Go API): {}", upstream_api);
    info!("Binding Web Gateway to: {}", listen_addr);

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .connect_timeout(Duration::from_secs(5))
        .build()
        .expect("Failed to build reqwest client");

    let state = Arc::new(AppState {
        api_upstream: upstream_api.trim_end_matches('/').to_string(),
        client,
    });

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods([
            Method::GET,
            Method::POST,
            Method::PUT,
            Method::DELETE,
            Method::OPTIONS,
            Method::PATCH,
        ])
        .allow_headers(Any);

    let app = Router::new()
        .route("/health", get(health_handler))
        .route("/healthz", get(health_handler))
        .route("/api/gateway", get(gateway_info_handler))
        .route("/api/*path", any(proxy_api_handler))
        .route("/api", any(proxy_api_handler))
        .fallback(static_handler)
        .layer(cors)
        .with_state(state);

    let addr: SocketAddr = listen_addr
        .parse()
        .unwrap_or_else(|_| "0.0.0.0:8088".parse().unwrap());

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    info!("Web Gateway & Dashboard listening at http://{}", addr);

    axum::serve(listener, app).await.unwrap();
}

async fn health_handler(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    let upstream_health_url = format!("{}/healthz", state.api_upstream);
    let upstream_ok = match state.client.get(&upstream_health_url).send().await {
        Ok(resp) => resp.status().is_success(),
        Err(_) => false,
    };

    (
        StatusCode::OK,
        Json(json!({
            "service": "atf-web-gateway",
            "version": "1.2.2",
            "status": "healthy",
            "upstream_api": state.api_upstream,
            "upstream_connected": upstream_ok
        })),
    )
}

async fn gateway_info_handler(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    Json(json!({
        "service": "atf-web-gateway",
        "version": "1.2.1",
        "status": "healthy",
        "role": "unified_web_gateway_bff",
        "upstream_api": state.api_upstream,
        "features": {
            "embedded_react_dashboard": true,
            "reverse_proxy_control_plane": true,
            "security_headers": true
        }
    }))
}

async fn proxy_api_handler(
    State(state): State<Arc<AppState>>,
    req: Request,
) -> impl IntoResponse {
    let method = req.method().clone();
    let uri = req.uri().clone();
    let path_and_query = uri.path_and_query().map(|pq| pq.as_str()).unwrap_or(uri.path());

    let target_url = format!("{}{}", state.api_upstream, path_and_query);

    let (parts, body) = req.into_parts();
    let body_bytes = match axum::body::to_bytes(body, 10 * 1024 * 1024).await {
        Ok(b) => b,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({
                    "error": "Failed to read request body",
                    "details": e.to_string()
                })),
            )
                .into_response();
        }
    };

    let mut upstream_req = state.client.request(method, &target_url);

    for (name, value) in parts.headers.iter() {
        if name != header::HOST && name != header::CONNECTION {
            upstream_req = upstream_req.header(name, value);
        }
    }

    if !body_bytes.is_empty() {
        upstream_req = upstream_req.body(body_bytes);
    }

    match upstream_req.send().await {
        Ok(resp) => {
            let status = StatusCode::from_u16(resp.status().as_u16())
                .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);

            let mut response_builder = Response::builder().status(status);

            for (name, value) in resp.headers().iter() {
                if name != header::TRANSFER_ENCODING && name != header::CONTENT_LENGTH {
                    response_builder = response_builder.header(name, value);
                }
            }

            match resp.bytes().await {
                Ok(bytes) => response_builder.body(axum::body::Body::from(bytes)).unwrap(),
                Err(e) => {
                    error!("Error reading upstream response body: {}", e);
                    (
                        StatusCode::BAD_GATEWAY,
                        Json(json!({
                            "error": "Failed to read response from upstream Control Plane",
                            "details": e.to_string()
                        })),
                    )
                        .into_response()
                }
            }
        }
        Err(e) => {
            warn!("Upstream Control Plane unreachable at {}: {}", target_url, e);
            (
                StatusCode::BAD_GATEWAY,
                Json(json!({
                    "error": "Upstream Control Plane (Go API) unreachable",
                    "service": "atf-web-gateway",
                    "upstream_url": target_url,
                    "details": e.to_string()
                })),
            )
                .into_response()
        }
    }
}

async fn static_handler(uri: Uri) -> impl IntoResponse {
    let raw_path = uri.path().trim_start_matches('/');
    let path = if raw_path.is_empty() {
        "index.html"
    } else {
        raw_path
    };

    match FrontendAssets::get(path) {
        Some(content) => {
            let mime = mime_guess::from_path(path).first_or_octet_stream();
            let cache_control = if path == "index.html" {
                "no-cache, no-store, must-revalidate"
            } else {
                "public, max-age=31536000, immutable"
            };
            Response::builder()
                .header(header::CONTENT_TYPE, mime.as_ref())
                .header(header::CACHE_CONTROL, cache_control)
                .body(axum::body::Body::from(content.data))
                .unwrap()
        }
        None => {
            // Never return index.html for missing assets/files with extension!
            if path.starts_with("assets/") || path.contains('.') {
                return (StatusCode::NOT_FOUND, format!("Asset not found: {}", path)).into_response();
            }
            // SPA fallback: return index.html with NO CACHE for clean navigation routes
            match FrontendAssets::get("index.html") {
                Some(content) => Response::builder()
                    .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
                    .header(header::CACHE_CONTROL, "no-cache, no-store, must-revalidate")
                    .body(axum::body::Body::from(content.data))
                    .unwrap(),
                None => (
                    StatusCode::NOT_FOUND,
                    Html("<h1>404 Not Found</h1><p>Embedded frontend assets missing.</p>"),
                )
                    .into_response(),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_embedded_assets_contain_index() {
        let index = FrontendAssets::get("index.html");
        assert!(index.is_some(), "index.html must be embedded in binary");
        let data = index.unwrap().data;
        let html = std::str::from_utf8(&data).unwrap();
        assert!(html.contains("<html") || html.contains("<!DOCTYPE") || html.contains("<div"));
    }
}
