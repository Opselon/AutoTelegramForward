//! The `/api` reverse proxy to the Go Control Plane.
//!
//! Request bodies **stream** to the upstream: the handler no longer buffers a
//! full 10 MB upload in memory before the first byte leaves the process. The
//! size cap is enforced two ways — an oversized `Content-Length` is refused
//! up front with 413, and the streamed body itself is wrapped in a counting
//! limiter for chunked uploads (which surfaces as 413 via the body-limit
//! error, even though bytes were already flowing).
//!
//! Hop-by-hop headers (RFC 7230 §6.1) are stripped on both legs — the old
//! code forwarded `Connection` and friends verbatim, which corrupts
//! keep-alive semantics between two independent HTTP connections.
//!
//! Timeouts: connect timeout up front, read timeout as the longest *idle gap*
//! mid-response. The previous blanket 30 s total timeout silently killed any
//! response that streamed longer than 30 s.
//!
//! Error bodies stay generic (`upstream_unavailable`): the specific reqwest
//! error text embeds the internal upstream URL and belongs in the logs —
//! tagged with the correlation id the client already holds — not in a
//! browser-facing JSON payload.

use std::sync::atomic::{AtomicU64, Ordering};

use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{header, HeaderMap, HeaderName, HeaderValue, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use futures_util::StreamExt;
use serde_json::json;
use tracing::Instrument;

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

/// Response headers the gateway owns. An upstream copy of any of these is
/// removed before the gateway asserts its own value, so a client never sees
/// two conflicting `X-Frame-Options` / `Access-Control-Allow-Origin` headers.
const GATEWAY_OWNED_RESPONSE_HEADERS: [HeaderName; 4] = [
    header::X_FRAME_OPTIONS,
    header::X_CONTENT_TYPE_OPTIONS,
    header::REFERRER_POLICY,
    HeaderName::from_static("access-control-allow-origin"),
];
/// Any other CORS header an upstream may emit is dropped as well — CORS on
/// `/api` is decided by this gateway's layer, nowhere else.
const GATEWAY_OWNED_CORS_PREFIXES: [&str; 2] = ["access-control-", "timing-allow-origin"];

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
                && s.chars().all(|c| {
                    c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.' || c == ':'
                })
        })
        .map(|s| s.to_string())
        .unwrap_or_else(new_correlation_id)
}

/// Generated fallback: wall-clock nanos (client-unpredictable) plus a
/// process-lifetime counter, so two requests inside one clock tick still get
/// distinct ids. Purely for correlation — not a secret, not a capability.
fn new_correlation_id() -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let seq = COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("corr-{nanos:016x}{seq:04x}")
}

/// Client key for the rate limiter. Default: the direct connection peer.
/// When the operator opts into a forwarded header, the **rightmost** address
/// wins: a proxy chain appends on the way in, so the rightmost entry is the
/// client as seen by the nearest trusted hop, while the leftmost entries pass
/// through attacker-controlled hands (a leftmost key would let a client mint
/// a fresh rate-limit bucket per request).
pub fn client_key(
    client_ip_header: &Option<String>,
    req_headers: &HeaderMap,
    peer: Option<std::net::SocketAddr>,
) -> String {
    if let Some(header_name) = client_ip_header {
        // Merge every value of the (possibly repeated) header, then take the
        // rightmost non-empty entry (the chain is double-ended, so scan from
        // the back instead of materialising everything for `last`).
        let rightmost = req_headers
            .get_all(header_name.as_str())
            .iter()
            .rev()
            .filter_map(|v| v.to_str().ok())
            .flat_map(|raw| raw.rsplit(','))
            .map(str::trim)
            .find(|entry| !entry.is_empty());
        if let Some(candidate) = rightmost {
            return candidate.to_string();
        }
    }
    peer.map(|addr| addr.ip().to_string())
        .unwrap_or_else(|| "unknown".to_string())
}

fn error_response(status: StatusCode, message: &str, correlation_id: &str) -> Response {
    (
        status,
        Json(json!({ "error": message, "correlation_id": correlation_id })),
    )
        .into_response()
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

/// Walk the reqwest error's source chain looking for the counting limiter's
/// size failure, so an over-limit *chunked* upload answers 413 (its honest
/// status) instead of a misleading 502 "unreachable".
fn is_body_limit_error(err: &dyn std::error::Error) -> bool {
    let mut source = Some(err);
    while let Some(current) = source {
        if current
            .to_string()
            .to_ascii_lowercase()
            .contains("length limit")
        {
            return true;
        }
        source = current.source();
    }
    false
}

pub async fn proxy_api_handler(State(state): State<AppState>, req: Request) -> Response {
    let peer = req
        .extensions()
        .get::<axum::extract::ConnectInfo<std::net::SocketAddr>>()
        .map(|info| info.0);
    let req_headers = req.headers().clone();
    let method = req.method().clone();
    let uri = req.uri().clone();

    let path_and_query = uri
        .path_and_query()
        .map(|pq| pq.as_str())
        .unwrap_or(uri.path())
        .to_string();
    let corr_id = correlation_id(&req_headers);

    // The span carries the correlation id, so every log line inside the
    // handler — including the 502 warning — is joinable with the id the
    // client and the control plane see.
    let span = tracing::info_span!(
        "api_proxy",
        correlation_id = %corr_id,
        method = %method,
        path = %path_and_query,
    );
    async move {
        proxy_request(
            state,
            req,
            req_headers,
            method,
            peer,
            path_and_query,
            corr_id,
        )
        .await
    }
    .instrument(span)
    .await
}

#[allow(clippy::too_many_arguments)]
async fn proxy_request(
    state: AppState,
    req: Request,
    req_headers: HeaderMap,
    method: Method,
    peer: Option<std::net::SocketAddr>,
    path_and_query: String,
    corr_id: String,
) -> Response {
    let started = std::time::Instant::now();

    // ---- rate limit (api surface only; static assets are exempt) ----------
    match state.rate_limiter.check(&client_key(
        &state.config.client_ip_header,
        &req_headers,
        peer,
    )) {
        Verdict::Allow => {}
        Verdict::Reject(wait) => {
            tracing::warn!(status = 429, "rate limited");
            let mut resp =
                error_response(StatusCode::TOO_MANY_REQUESTS, "Too many requests", &corr_id);
            if let Ok(value) = HeaderValue::from_str(&wait.as_secs().to_string()) {
                resp.headers_mut().insert(header::RETRY_AFTER, value);
            }
            return resp;
        }
    }

    let target_url = format!("{}{}", state.config.upstream_api, path_and_query);

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
                &corr_id,
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
        // Binary-safe: forward the original header values verbatim instead of
        // round-tripping them through UTF-8 (which silently dropped any
        // header carrying opaque bytes).
        for (name, value) in forward_headers.iter() {
            upstream_req = upstream_req.header(name, value.clone());
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
                    tracing::warn!("upstream connect failed, retrying once: {e}");
                    continue;
                }
                return bad_gateway(&e, &corr_id);
            }
            Err(e) => {
                if is_body_limit_error(&e) {
                    tracing::warn!(
                        status = 413,
                        "streamed request body exceeded the size limit"
                    );
                    return error_response(
                        StatusCode::PAYLOAD_TOO_LARGE,
                        "Request body too large",
                        &corr_id,
                    );
                }
                return bad_gateway(&e, &corr_id);
            }
        }
    }
    let upstream_resp = response.expect("loop returns on error; success always sets response");

    let status = StatusCode::from_u16(upstream_resp.status().as_u16())
        .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    tracing::debug!(
        status = status.as_u16(),
        duration_ms = started.elapsed().as_millis() as u64,
        "proxied"
    );

    // ---- response: gateway-owned headers re-asserted, hop-by-hop stripped -
    let mut out_headers = upstream_resp.headers().clone();
    strip_hop_by_hop(&mut out_headers);
    out_headers.remove(header::CONTENT_LENGTH); // body is re-streamed
    for name in GATEWAY_OWNED_RESPONSE_HEADERS.iter() {
        out_headers.remove(name);
    }
    for prefix in GATEWAY_OWNED_CORS_PREFIXES {
        let owned: Vec<HeaderName> = out_headers
            .keys()
            .filter(|name| name.as_str().starts_with(prefix))
            .cloned()
            .collect();
        for name in owned {
            out_headers.remove(&name);
        }
    }

    let mut response_builder = Response::builder()
        .status(status)
        .header("X-Content-Type-Options", "nosniff")
        .header("X-Frame-Options", "DENY")
        .header("Referrer-Policy", "strict-origin-when-cross-origin")
        .header("X-Correlation-ID", &corr_id)
        .header("X-Request-ID", &corr_id);
    for (name, value) in out_headers.iter() {
        response_builder = response_builder.header(name, value);
    }

    let resp_body = Body::from_stream(upstream_resp.bytes_stream());
    match response_builder.body(resp_body) {
        Ok(resp) => resp,
        Err(e) => {
            tracing::error!("failed to construct proxied response: {e}");
            error_response(
                StatusCode::BAD_GATEWAY,
                "Failed to construct proxied response",
                &corr_id,
            )
        }
    }
}

/// 502 with a generic body: the reqwest error text embeds the internal
/// upstream URL, so details go to the (correlation-tagged) log, not the wire.
fn bad_gateway(err: &reqwest::Error, corr_id: &str) -> Response {
    tracing::warn!(status = 502, "upstream request failed: {err}");
    error_response(
        StatusCode::BAD_GATEWAY,
        "Upstream Control Plane (Go API) unreachable",
        corr_id,
    )
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
            assert!(
                headers.get(gone).is_none(),
                "{gone} must not cross the proxy"
            );
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
            assert!(
                id.starts_with("corr-"),
                "{bad:?} must fall back to a generated id"
            );
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
    fn generated_correlation_ids_are_unique() {
        // Two ids minted inside the same clock tick must still differ: the
        // process counter disambiguates what nanos alone could not.
        let a = new_correlation_id();
        let b = new_correlation_id();
        assert_ne!(a, b);
        assert!(a.starts_with("corr-"));
    }

    #[test]
    fn body_declaration_rules() {
        assert!(!request_declares_body(&HeaderMap::new()), "bodyless GET");
        assert!(request_declares_body(&header_map(&[(
            "Content-Length",
            "14"
        )])));
        assert!(!request_declares_body(&header_map(&[(
            "Content-Length",
            "0"
        )])));
        assert!(request_declares_body(&header_map(&[(
            "Transfer-Encoding",
            "chunked"
        )])));
    }

    #[test]
    fn client_key_takes_the_rightmost_forwarded_entry() {
        let headers = header_map(&[("X-Forwarded-For", "203.0.113.9, 198.51.100.4")]);
        let key = client_key(&Some("X-Forwarded-For".to_string()), &headers, None);
        assert_eq!(
            key, "198.51.100.4",
            "the rightmost entry is the nearest trusted hop's view; leftmost is client-controlled"
        );
    }

    #[test]
    fn client_key_merges_repeated_forwarded_headers() {
        let mut headers = HeaderMap::new();
        headers.insert(
            HeaderName::from_static("x-forwarded-for"),
            HeaderValue::from_static("203.0.113.9"),
        );
        headers.append(
            HeaderName::from_static("x-forwarded-for"),
            HeaderValue::from_static("198.51.100.4"),
        );
        let key = client_key(&Some("X-Forwarded-For".to_string()), &headers, None);
        assert_eq!(
            key, "198.51.100.4",
            "the last value of the last header wins"
        );
    }

    #[test]
    fn client_key_falls_back_to_the_peer() {
        let headers = header_map(&[("X-Forwarded-For", "203.0.113.9")]);
        let peer: std::net::SocketAddr = "192.0.2.10:55000".parse().unwrap();
        // No header configured -> peer, whatever the request carries.
        assert_eq!(client_key(&None, &headers, Some(peer)), "192.0.2.10");
        // Configured header present but empty -> peer.
        let empty = header_map(&[("X-Forwarded-For", "  ")]);
        assert_eq!(
            client_key(&Some("X-Forwarded-For".to_string()), &empty, Some(peer)),
            "192.0.2.10"
        );
        // Nothing at all -> the stable "unknown" bucket.
        assert_eq!(client_key(&None, &HeaderMap::new(), None), "unknown");
    }

    #[test]
    fn body_limit_errors_are_recognised_through_the_chain() {
        // Mirror the real wrapping: the limiter's SizeLimitError is stored as
        // the source of the io::Error that the stream mapper produces.
        let inner: Box<dyn std::error::Error + Send + Sync> = "length limit exceeded".into();
        let io = std::io::Error::other(inner);
        assert!(
            is_body_limit_error(&io),
            "the limiter's text must be found through io::Error"
        );
        let plain: Box<dyn std::error::Error + Send + Sync> = "connection refused".into();
        let io2 = std::io::Error::other(plain);
        assert!(!is_body_limit_error(&io2));
    }
}
