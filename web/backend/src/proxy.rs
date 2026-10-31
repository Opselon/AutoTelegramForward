//! The `/api` reverse proxy to the Go Control Plane.
//!
//! Request bodies **stream** to the upstream: the handler no longer buffers a
//! full 10 MB upload in memory before the first byte leaves the process. The
//! size cap is enforced two ways — an oversized `Content-Length` is refused
//! up front with 413, and the streamed body itself is wrapped in a counting
//! limiter for chunked uploads (whose failure surfaces as a dropped request
//! once bytes are already flowing — the only honest answer at that point).
//!
//! Hop-by-hop headers (RFC 7230 §6.1) are stripped on both legs — the old
//! code forwarded `Connection` and friends verbatim, which corrupts
//! keep-alive semantics between two independent HTTP connections.
//!
//! Timeouts: connect timeout up front, read timeout as the longest *idle gap*
//! mid-response. The previous blanket 30 s total timeout silently killed any
//! response that streamed longer than 30 s.

use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{header, HeaderMap, HeaderName, HeaderValue, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use futures_util::StreamExt;
use serde_json::json;

use crate::rate_limit::Verdict;
use crate::state::AppState;

/// RFC 7230 §6.1: these names never cross a proxy, whatever `Connection` says.
/// (`keep-alive` has no constant in the `http` crate — it is built from static.)
const HOP_BY_HOP: [HeaderName; 8] = [
    header::CONNECTION,
    HeaderName::from_static("keep-alive"),
    header::PROXY_AUTHENTICATE,
    header::PROXY_AUTHORIZATION,
    header::TE,
    header::TRAILER,
    header::TRANSFER_ENCODING,
    header::UPGRADE,
];

/// Remove hop-by-hop headers plus any named in a `Connection` header.
pub fn strip_hop_by_hop(headers: &mut HeaderMap) {
    let mut extra: Vec<HeaderName> = Vec::new();
    if let Some(Ok(connection)) = headers.get(header::CONNECTION).map(|v| v.to_str()) {
        for name in connection.split(',') {
            if let Ok(parsed) = name.trim().parse::<HeaderName>() {
                extra.push(parsed);
            }
        }
    }
    for name in HOP_BY_HOP.iter().chain(extra.iter()) {
        headers.remove(name);
    }
}

/// Extract or generate the correlation id threaded through gateway → control
/// plane → logs. An inbound id is honoured only when it is sane (length and
/// alphabet checked): a client-chosen id lands in our logs and the control
/// plane's, so it cannot double as a header-injection channel.
pub fn correlation_id(headers: &HeaderMap) -> String {
    headers
        .get("x-correlation-id")
        .or_else(|| headers.get("x-request-id"))
        .or_else(|| headers.get("traceparent"))
        .and_then(|v| v.to_str().ok())
        .filter(|s| {
            s.len() >= 8
                && s.len() <= 128
                && s.chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.' || c == ':')
        })
        .map(|s| s.to_string())
        .unwrap_or_else(|| {
            let nanos = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos();
            format!("corr-{nanos:016x}")
        })
}

/// Client key for the rate limiter: the configured forwarded-IP header's first
/// address, else the direct connection peer.
fn client_key(
    client_ip_header: &Option<String>,
    req_headers: &HeaderMap,
    peer: Option<std::net::SocketAddr>,
) -> String {
    if let Some(header_name) = client_ip_header {
        if let Some(Ok(raw)) = req_headers.get(header_name.as_str()).map(|v| v.to_str()) {
            if let Some(first) = raw.split(',').next() {
                let candidate = first.trim();
                if !candidate.is_empty() {
                    return candidate.to_string();
                }
            }
        }
    }
    peer.map(|addr| addr.ip().to_string())
        .unwrap_or_else(|| "unknown".to_string())
}

fn error_response(status: StatusCode, message: &str, details: String) -> Response {
    (status, Json(json!({ "error": message, "details": details }))).into_response()
}

/// Whether the inbound request declares a body worth forwarding. Attaching a
/// stream body to a bodyless GET would force chunked framing onto it; the
/// clean rule is "Content-Length > 0, or the request says chunked".
fn request_declares_body(headers: &HeaderMap) -> bool {
    let content_length = headers
        .get(header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(0);
    content_length > 0
        || headers
            .get(header::TRANSFER_ENCODING)
            .and_then(|v| v.to_str().ok())
            .map(|v| v.to_ascii_lowercase().contains("chunked"))
            .unwrap_or(false)
}

pub async fn proxy_api_handler(State(state): State<AppState>, req: Request) -> Response {
    let peer = req
        .extensions()
        .get::<axum::extract::ConnectInfo<std::net::SocketAddr>>()
        .map(|info| info.0);
    let req_headers = req.headers().clone();
    let method = req.method().clone();
    let uri = req.uri().clone();

    // ---- rate limit (api surface only; static assets are exempt) ----------
    match state.rate_limiter.check(&client_key(&state.config.client_ip_header, &req_headers, peer)) {
        Verdict::Allow => {}
        Verdict::Reject(wait) => {
            let mut resp = error_response(
                StatusCode::TOO_MANY_REQUESTS,
                "Too many requests",
                format!("retry after {:?}", wait),
            );
            if let Ok(value) = HeaderValue::from_str(&wait.as_secs().to_string()) {
                resp.headers_mut().insert(header::RETRY_AFTER, value);
            }
            return resp;
        }
    }

    let path_and_query = uri
        .path_and_query()
        .map(|pq| pq.as_str())
        .unwrap_or(uri.path());
    let target_url = format!("{}{}", state.config.upstream_api, path_and_query);
    let corr_id = correlation_id(&req_headers);

    // ---- outbound header set (minus hop-by-hop, minus host/length) --------
    let mut forward_headers = req_headers.clone();
    strip_hop_by_hop(&mut forward_headers);
    forward_headers.remove(header::HOST);
    // Framing is reqwest's job: the body leaves as a stream (chunked) while
    // the inbound header says content-length — forwarding both is a hyper
    // error. The bytes are identical; only the framing vocabulary changes.
    forward_headers.remove(header::CONTENT_LENGTH);

    // ---- body: refuse oversized up front, stream the rest -----------------
    let content_length = req_headers
        .get(header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<usize>().ok());
    if let Some(len) = content_length {
        if len > state.config.max_body_bytes {
            return error_response(
                StatusCode::PAYLOAD_TOO_LARGE,
                "Request body too large",
                format!("{len} bytes exceeds the {} byte limit", state.config.max_body_bytes),
            );
        }
    }

    // `Limited` counts every frame the stream yields, so a chunked upload with
    // no Content-Length is capped too.
    let forwarding_body = request_declares_body(&req_headers);
    let body = if forwarding_body {
        let limited = axum::body::Body::new(http_body_util::Limited::new(
            req.into_body(),
            state.config.max_body_bytes,
        ));
        Some(reqwest::Body::wrap_stream(
            limited
                .into_data_stream()
                .map(|chunk| chunk.map_err(std::io::Error::other)),
        ))
    } else {
        // Drop the receiver so the connection's read half is released.
        drop(req);
        None
    };

    // ---- send, with one connect-retry for idempotent bodyless requests ----
    // A streaming body is consumed once (reqwest bodies are not cloneable), so
    // only bodyless requests are retry candidates — which is exactly the safe
    // set anyway: a connect error on a bodyless request guarantees the
    // upstream never saw any of it.
    let mut body = body;
    let mut response = None;
    for attempt in 0..2usize {
        let mut upstream_req = state.client.request(method.clone(), &target_url);
        for (name, value) in forward_headers.iter() {
            if let Ok(value) = value.to_str() {
                upstream_req = upstream_req.header(name, value);
            }
        }
        upstream_req = upstream_req
            .header("X-Correlation-ID", &corr_id)
            .header("X-Request-ID", &corr_id);
        if let Some(body) = body.take() {
            upstream_req = upstream_req.body(body);
        }

        match upstream_req.send().await {
            Ok(resp) => {
                response = Some(resp);
                break;
            }
            Err(e) if attempt == 0 && e.is_connect() && !forwarding_body => {
                // A connect error on a bodyless idempotent request is safe to
                // replay: the upstream cannot have seen any of it.
                if matches!(method, Method::GET | Method::HEAD) {
                    tracing::warn!(target = %target_url, "upstream connect failed, retrying once: {e}");
                    continue;
                }
                return error_response(
                    StatusCode::BAD_GATEWAY,
                    "Upstream Control Plane (Go API) unreachable",
                    e.to_string(),
                );
            }
            Err(e) => {
                tracing::warn!(target = %target_url, "Upstream request failed: {e}");
                return error_response(
                    StatusCode::BAD_GATEWAY,
                    "Upstream Control Plane (Go API) unreachable",
                    e.to_string(),
                );
            }
        }
    }
    let upstream_resp = response.expect("loop returns on error; success always sets response");

    let status = StatusCode::from_u16(upstream_resp.status().as_u16())
        .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);

    // ---- response: security headers re-asserted, hop-by-hop stripped ------
    let mut response_builder = Response::builder()
        .status(status)
        .header("X-Content-Type-Options", "nosniff")
        .header("X-Frame-Options", "DENY")
        .header("Referrer-Policy", "strict-origin-when-cross-origin")
        .header("X-Correlation-ID", &corr_id)
        .header("X-Request-ID", &corr_id);

    let mut out_headers = upstream_resp.headers().clone();
    strip_hop_by_hop(&mut out_headers);
    out_headers.remove(header::CONTENT_LENGTH); // body is re-streamed
    for (name, value) in out_headers.iter() {
        response_builder = response_builder.header(name, value);
    }

    let resp_body = Body::from_stream(upstream_resp.bytes_stream());
    match response_builder.body(resp_body) {
        Ok(resp) => resp,
        Err(e) => error_response(
            StatusCode::BAD_GATEWAY,
            "Failed to construct proxied response",
            e.to_string(),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn header_map(entries: &[(&str, &str)]) -> HeaderMap {
        let mut map = HeaderMap::new();
        for (name, value) in entries {
            map.insert(
                HeaderName::from_bytes(name.as_bytes()).unwrap(),
                HeaderValue::from_str(value).unwrap(),
            );
        }
        map
    }

    #[test]
    fn hop_by_hop_headers_are_stripped() {
        let mut headers = header_map(&[
            ("Connection", "keep-alive, X-Private"),
            ("Keep-Alive", "timeout=5"),
            ("Transfer-Encoding", "chunked"),
            ("X-Correlation-ID", "corr-abc"),
            ("X-Private", "secret"),
            ("Content-Type", "application/json"),
        ]);
        strip_hop_by_hop(&mut headers);
        for gone in ["connection", "keep-alive", "transfer-encoding", "x-private"] {
            assert!(headers.get(gone).is_none(), "{gone} must not cross the proxy");
        }
        assert!(headers.get("x-correlation-id").is_some());
        assert!(headers.get("content-type").is_some());
    }

    #[test]
    fn correlation_id_honours_sane_inbound() {
        let headers = header_map(&[("X-Correlation-ID", "abc-123_ABC.09")]);
        assert_eq!(correlation_id(&headers), "abc-123_ABC.09");
    }

    #[test]
    fn correlation_id_rejects_injection_shapes() {
        for bad in [
            "short",                             // < 8 chars
            &"x".repeat(129),                    // > 128 chars
            "has space and-more-than-eight",     // disallowed chars
            "semicolon;injection:attempt-extra", // `;` not allowed
        ] {
            let headers = header_map(&[("X-Correlation-ID", bad)]);
            let id = correlation_id(&headers);
            assert!(id.starts_with("corr-"), "{bad:?} must fall back to a generated id");
        }
    }

    #[test]
    fn correlation_id_prefers_correlation_over_request_id() {
        let headers = header_map(&[
            ("X-Correlation-ID", "from-correlation-header"),
            ("X-Request-ID", "from-request-header"),
        ]);
        assert_eq!(correlation_id(&headers), "from-correlation-header");
    }

    #[test]
    fn body_declaration_rules() {
        assert!(!request_declares_body(&HeaderMap::new()), "bodyless GET");
        assert!(request_declares_body(&header_map(&[("Content-Length", "14")])));
        assert!(!request_declares_body(&header_map(&[("Content-Length", "0")])));
        assert!(request_declares_body(&header_map(&[("Transfer-Encoding", "chunked")])));
    }
}
