//! Environment-driven configuration, validated once at boot.
//!
//! Every knob the gateway honours lives here so operators (and the
//! docker-compose / k8s / systemd launchers) have one list to read. Invalid
//! values refuse to start the process — a silently defaulted `ATF_API_ADDR`
//! once made the gateway proxy into a different control plane than the one the
//! operator believed they had configured.

use std::net::SocketAddr;

/// The canonical env var across the whole stack (docker-compose, k8s, atf.py,
/// systemd unit) is `ATF_API_ADDR`; the older `ATF_UPSTREAM_API` / `ATF_API_URL`
/// aliases stay as a courtesy.
const UPSTREAM_ENV_VARS: [&str; 3] = ["ATF_API_ADDR", "ATF_UPSTREAM_API", "ATF_API_URL"];

#[derive(Debug, Clone)]
pub struct Config {
    /// Local bind address, e.g. `0.0.0.0:8088`.
    pub listen_addr: SocketAddr,
    /// Upstream Control Plane (Go API) base URL, no trailing slash.
    pub upstream_api: String,
    /// Client-IP header used by the rate limiter. Empty = trust `RemoteAddr`.
    pub client_ip_header: Option<String>,
    /// Token-bucket refill rate, requests per second per client.
    pub rate_limit_rps: f64,
    /// Token-bucket burst size (the bucket's capacity).
    pub rate_limit_burst: u32,
    /// Requests over this many bytes are refused with 413 before streaming.
    pub max_body_bytes: usize,
    /// Upstream connect timeout.
    pub upstream_connect_timeout_secs: u64,
    /// Upstream read timeout: the longest idle gap allowed mid-response. This
    /// deliberately replaced a blanket 30 s total timeout that killed any
    /// long-lived streaming response.
    pub upstream_read_timeout_secs: u64,
    /// Comma-separated CORS allow-list. Empty = permissive (documented
    /// backwards compatibility), which logs a warning at boot.
    pub allowed_origins: Vec<String>,
    /// Full `Content-Security-Policy` override. `None` = derive a strict
    /// policy from the embedded index.html (see `security.rs`).
    pub csp: Option<String>,
    /// `json` for machine-parseable logs (collectors), anything else = pretty.
    pub log_format_json: bool,
}

impl Config {
    /// Read the environment. Errors name the offending variable so an operator
    /// sees exactly which value was rejected.
    pub fn from_env() -> Result<Self, String> {
        let listen_addr_raw =
            env_var("ATF_WEB_ADDR").unwrap_or_else(|| "0.0.0.0:8088".to_string());
        let listen_addr: SocketAddr = listen_addr_raw
            .parse()
            .map_err(|_| format!("ATF_WEB_ADDR={listen_addr_raw:?} is not a valid socket address"))?;

        let mut upstream_api: Option<String> = None;
        for var in UPSTREAM_ENV_VARS {
            if let Ok(raw) = std::env::var(var) {
                let trimmed = raw.trim().trim_end_matches('/').to_string();
                if !trimmed.is_empty() {
                    upstream_api = Some(trimmed);
                    break;
                }
            }
        }
        let upstream_api = upstream_api.unwrap_or_else(|| "http://127.0.0.1:8080".to_string());
        if !(upstream_api.starts_with("http://") || upstream_api.starts_with("https://")) {
            return Err(format!(
                "ATF_API_ADDR must start with http:// or https:// (got {upstream_api:?})"
            ));
        }

        let ip_header = match std::env::var("ATF_WEB_CLIENT_IP_HEADER") {
            Ok(raw) if !raw.trim().is_empty() => Some(raw.trim().to_string()),
            // Backwards-compatible default: behind the Caddy TLS profile the
            // direct peer is the reverse proxy, so the forwarded header is the
            // only per-client signal. Documented spoofing caveat: an attacker
            // connecting directly can set it too, which at worst skews a
            // best-effort rate limit — it guards no security boundary.
            _ => Some("X-Forwarded-For".to_string()),
        };

        let rate_limit_rps = env_parse("ATF_WEB_RATELIMIT_RPS", 20.0_f64)?;
        if !(rate_limit_rps.is_finite() && rate_limit_rps > 0.0) {
            return Err(format!("ATF_WEB_RATELIMIT_RPS must be > 0 (got {rate_limit_rps})"));
        }
        let rate_limit_burst: u32 = env_parse("ATF_WEB_RATELIMIT_BURST", 40u32)?;
        if rate_limit_burst == 0 {
            return Err("ATF_WEB_RATELIMIT_BURST must be > 0".to_string());
        }

        let max_body_bytes: usize = env_parse("ATF_WEB_MAX_BODY_BYTES", 10 * 1024 * 1024usize)?;

        let upstream_connect_timeout_secs: u64 = env_parse("ATF_WEB_UPSTREAM_CONNECT_TIMEOUT_SECS", 5u64)?;
        let upstream_read_timeout_secs: u64 = env_parse("ATF_WEB_UPSTREAM_READ_TIMEOUT_SECS", 30u64)?;

        let allowed_origins = std::env::var("ATF_WEB_ALLOWED_ORIGINS")
            .map(|raw| {
                raw.split(',')
                    .map(|o| o.trim().trim_end_matches('/').to_string())
                    .filter(|o| !o.is_empty())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();

        let csp = std::env::var("ATF_WEB_CSP")
            .ok()
            .map(|raw| raw.trim().to_string())
            .filter(|raw| !raw.is_empty());

        let log_format_json = std::env::var("ATF_WEB_LOG_FORMAT")
            .map(|v| v.trim().eq_ignore_ascii_case("json"))
            .unwrap_or(false);

        Ok(Self {
            listen_addr,
            upstream_api,
            client_ip_header: ip_header,
            rate_limit_rps,
            rate_limit_burst,
            max_body_bytes,
            upstream_connect_timeout_secs,
            upstream_read_timeout_secs,
            allowed_origins,
            csp,
            log_format_json,
        })
    }
}

fn env_var(name: &str) -> Option<String> {
    std::env::var(name).ok().map(|raw| raw.trim().to_string()).filter(|raw| !raw.is_empty())
}

/// Parse one env var into `T`, falling back to `default` when unset or blank.
/// A present-but-unparsable value is a hard error, never a silent default.
fn env_parse<T: std::str::FromStr>(name: &str, default: T) -> Result<T, String> {
    match env_var(name) {
        None => Ok(default),
        Some(raw) => raw.parse::<T>().map_err(|_| {
            format!("{name}={raw:?} does not parse as {}", std::any::type_name::<T>())
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // The env is process-global, so every scenario runs inside ONE #[test] to
    // stay immune to cargo's parallel test threads.
    #[test]
    fn config_env_contract() {
        let clear = || {
            for k in [
                "ATF_WEB_ADDR",
                "ATF_API_ADDR",
                "ATF_UPSTREAM_API",
                "ATF_WEB_CLIENT_IP_HEADER",
                "ATF_WEB_RATELIMIT_RPS",
                "ATF_WEB_RATELIMIT_BURST",
                "ATF_WEB_MAX_BODY_BYTES",
                "ATF_WEB_UPSTREAM_CONNECT_TIMEOUT_SECS",
                "ATF_WEB_UPSTREAM_READ_TIMEOUT_SECS",
                "ATF_WEB_ALLOWED_ORIGINS",
                "ATF_WEB_CSP",
                "ATF_WEB_LOG_FORMAT",
            ] {
                std::env::remove_var(k);
            }
        };

        clear();
        let cfg = Config::from_env().expect("defaults parse");
        assert_eq!(cfg.listen_addr.to_string(), "0.0.0.0:8088");
        assert_eq!(cfg.upstream_api, "http://127.0.0.1:8080");
        assert_eq!(cfg.rate_limit_rps, 20.0);
        assert_eq!(cfg.rate_limit_burst, 40);
        assert_eq!(cfg.client_ip_header.as_deref(), Some("X-Forwarded-For"));
        assert!(cfg.csp.is_none());
        assert!(!cfg.log_format_json);

        clear();
        std::env::set_var("ATF_UPSTREAM_API", "http://api:8080/");
        std::env::set_var("ATF_WEB_ALLOWED_ORIGINS", "https://a.example , https://b.example/");
        std::env::set_var("ATF_WEB_LOG_FORMAT", "json");
        let cfg = Config::from_env().unwrap();
        assert_eq!(cfg.upstream_api, "http://api:8080", "trailing slash trimmed");
        assert_eq!(cfg.allowed_origins, vec!["https://a.example", "https://b.example"]);
        assert!(cfg.log_format_json);

        clear();
        std::env::set_var("ATF_WEB_RATELIMIT_RPS", "zero");
        assert!(Config::from_env().is_err(), "non-numeric rps must refuse to start");
        clear();
        std::env::set_var("ATF_API_ADDR", "api:8080");
        assert!(Config::from_env().is_err(), "upstream without a scheme must refuse");
        clear();
    }
}
