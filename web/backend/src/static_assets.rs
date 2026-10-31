//! Embedded SPA serving: hashed assets get immutable caching, index.html
//! never caches, deep links fall back to index.html, and every response
//! carries the security header set (the CSP included).
//!
//! Cache contract:
//! - `assets/*` — Vite emits content-hashed filenames there, so they are
//!   immutable for a year;
//! - other non-index files (`favicon.ico`, `site.webmanifest`, …) have no
//!   fingerprint, so they get a short `max-age` instead of being pinned for
//!   a year;
//! - `index.html` and SPA deep-link fallbacks — `no-cache` so a new deploy is
//!   picked up on the next navigation.

use axum::http::{header, response::Builder, uri::Uri, StatusCode};
use axum::response::{Html, IntoResponse, Response};
use rust_embed::RustEmbed;

use crate::security::STATIC_SECURITY_HEADERS;

/// Vite's content-hashed asset directory: safe to cache immutably.
const FINGERPRINTED_PREFIX: &str = "assets/";

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

/// Cache-Control for a successfully served embedded file.
fn cache_control_for(path: &str) -> &'static str {
    if path == "index.html" {
        "no-cache, no-store, must-revalidate"
    } else if path.starts_with(FINGERPRINTED_PREFIX) {
        // Content-hashed filename: new content always means a new URL.
        "public, max-age=31536000, immutable"
    } else {
        // Un-fingerprinted files must stay updatable: one hour, revalidated.
        "public, max-age=3600, must-revalidate"
    }
}

pub fn serve(uri: Uri, csp: &str) -> Response {
    let raw_path = uri.path().trim_start_matches('/');
    let path = if raw_path.is_empty() {
        "index.html"
    } else {
        raw_path
    };

    match FrontendAssets::get(path) {
        Some(content) => {
            let mime = mime_guess::from_path(path).first_or_octet_stream();
            let mut builder = security_headers(
                Response::builder()
                    .header(header::CONTENT_TYPE, mime.as_ref())
                    .header(header::CACHE_CONTROL, cache_control_for(path)),
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
            // Never return index.html for missing assets/files with extension
            // or under the fingerprinted directory: a missing bundle must not
            // masquerade as a valid page (and must not echo the requested
            // path back, either).
            if path.starts_with(FINGERPRINTED_PREFIX) || path.contains('.') {
                return security_headers(Response::builder().status(StatusCode::NOT_FOUND), csp)
                    .body(axum::body::Body::from("Not found"))
                    .unwrap();
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

#[cfg(test)]
mod tests {
    use super::*;

    fn embedded_available() -> bool {
        FrontendAssets::get("index.html").is_some()
    }

    fn uri_of(path: &str) -> Uri {
        path.parse().unwrap()
    }

    async fn body_text(resp: Response) -> String {
        let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
            .await
            .unwrap();
        String::from_utf8_lossy(&bytes).to_string()
    }

    #[test]
    fn cache_control_policy_per_file_class() {
        assert_eq!(
            cache_control_for("index.html"),
            "no-cache, no-store, must-revalidate"
        );
        assert_eq!(
            cache_control_for("assets/index-D6KaiRQa.js"),
            "public, max-age=31536000, immutable",
            "content-hashed assets are immutable"
        );
        assert_eq!(
            cache_control_for("favicon.ico"),
            "public, max-age=3600, must-revalidate",
            "un-fingerprinted files must stay updatable"
        );
    }

    #[tokio::test]
    async fn missing_fingerprinted_asset_is_a_headered_404_without_path_echo() {
        if !embedded_available() {
            eprintln!("skipping: frontend dist not built");
            return;
        }
        let resp = serve(
            uri_of("/assets/no-such-bundle-abc123.js"),
            "default-src 'self'",
        );
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
        let headers = resp.headers();
        assert_eq!(headers.get("x-content-type-options").unwrap(), "nosniff");
        assert!(headers.get("content-security-policy").is_some());
        let text = body_text(resp).await;
        assert!(
            !text.contains("no-such-bundle"),
            "the 404 body must not reflect the requested path: {text:?}"
        );
    }

    #[tokio::test]
    async fn deep_link_falls_back_to_index_html() {
        if !embedded_available() {
            eprintln!("skipping: frontend dist not built");
            return;
        }
        let resp = serve(uri_of("/some/deep/link"), "default-src 'self'");
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(
            resp.headers().get("cache-control").unwrap(),
            "no-cache, no-store, must-revalidate"
        );
        assert!(
            resp.headers().get("x-frame-options").is_none(),
            "TMA framing stays possible"
        );
    }
}
