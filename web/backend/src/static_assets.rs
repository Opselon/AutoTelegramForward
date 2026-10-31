//! Embedded SPA serving: hashed assets get immutable caching, index.html
//! never caches, deep links fall back to index.html, and every response
//! carries the security header set (the CSP included).

use axum::http::{header, response::Builder, uri::Uri, StatusCode};
use axum::response::{Html, IntoResponse, Response};
use rust_embed::RustEmbed;

use crate::security::STATIC_SECURITY_HEADERS;

#[derive(RustEmbed)]
#[folder = "../frontend/dist"]
pub struct FrontendAssets;

/// The security header set every SPA response carries (the CSP included).
fn security_headers(builder: Builder, csp: &str) -> Builder {
    let mut builder = builder.header("Content-Security-Policy", csp);
    for (name, value) in STATIC_SECURITY_HEADERS {
        builder = builder.header(name, value);
    }
    builder
}

pub fn serve(uri: Uri, csp: &str) -> Response {
    let raw_path = uri.path().trim_start_matches('/');
    let path = if raw_path.is_empty() { "index.html" } else { raw_path };

    match FrontendAssets::get(path) {
        Some(content) => {
            let mime = mime_guess::from_path(path).first_or_octet_stream();
            let cache_control = if path == "index.html" {
                "no-cache, no-store, must-revalidate"
            } else {
                "public, max-age=31536000, immutable"
            };
            let mut builder = security_headers(
                Response::builder()
                    .header(header::CONTENT_TYPE, mime.as_ref())
                    .header(header::CACHE_CONTROL, cache_control),
                csp,
            );
            // index.html is the one HTML entry and TMA may frame it; other
            // HTML assets are inert. DENY there keeps the old intent without
            // breaking Telegram Mini App embedding.
            if path != "index.html" && mime.type_() == "text" && mime.subtype() == "html" {
                builder = builder.header("X-Frame-Options", "DENY");
            }
            builder.body(axum::body::Body::from(content.data)).unwrap()
        }
        None => {
            // Never return index.html for missing assets/files with extension.
            if path.starts_with("assets/") || path.contains('.') {
                return (StatusCode::NOT_FOUND, format!("Asset not found: {path}")).into_response();
            }
            // SPA fallback: deep links render index.html with NO CACHE.
            match FrontendAssets::get("index.html") {
                Some(content) => security_headers(
                    Response::builder()
                        .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
                        .header(header::CACHE_CONTROL, "no-cache, no-store, must-revalidate"),
                    csp,
                )
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

/// Compression applies to the static surface only: the assets are static text
/// that shrinks 3–4×, while a proxied streaming response (or SSE) must not be
/// buffered by a compression layer.
pub fn compression_layer() -> tower_http::compression::CompressionLayer {
    tower_http::compression::CompressionLayer::new()
        .gzip(true)
        .br(true)
}
