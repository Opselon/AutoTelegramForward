//! Shared handler state: the validated config, one tuned HTTP client, the
//! rate limiter, and a short-lived cache for the upstream health probe.
//! Cloned cheaply behind an `Arc` per request.

use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::RwLock;

use crate::config::Config;
use crate::rate_limit::RateLimiter;

/// How long a cached upstream-probe verdict stays trustworthy. Health checks
/// arrive every few seconds (docker) and on every dashboard load; without the
/// cache each one waits on a live upstream round-trip.
pub const UPSTREAM_PROBE_TTL: Duration = Duration::from_secs(2);

pub struct GatewayState {
    pub config: Config,
    pub client: reqwest::Client,
    pub rate_limiter: RateLimiter,
    /// Content-Security-Policy resolved at boot: the operator's explicit value,
    /// or the one derived from the embedded index.html.
    pub csp: String,
    /// Last upstream-probe verdict with its timestamp, so `/health` bursts do
    /// not each pay a fresh upstream round-trip.
    pub upstream_probe: RwLock<Option<(Instant, bool)>>,
}

pub type AppState = Arc<GatewayState>;

/// Build the upstream client. Deliberate choices:
/// - `connect_timeout` bounds the dial only;
/// - `read_timeout` bounds each *idle gap* on the connection, so a slow but
///   live streaming response survives while a hung one is cut — the blanket
///   total `timeout(30 s)` this replaces used to kill long-lived responses;
/// - connection pooling keeps the per-request TLS/handshake cost off the
///   dashboard's 3 s log poll.
///
/// Returns a config error instead of panicking: a rejected builder value is
/// an operator-visible misconfiguration, not a bug.
pub fn build_client(config: &Config) -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(config.upstream_connect_timeout_secs))
        .read_timeout(Duration::from_secs(config.upstream_read_timeout_secs))
        .pool_idle_timeout(Duration::from_secs(90))
        .pool_max_idle_per_host(32)
        .build()
        .map_err(|e| format!("upstream HTTP client rejected the configuration: {e}"))
}

pub fn build_state(config: Config, csp: String) -> Result<AppState, String> {
    let client = build_client(&config)?;
    Ok(Arc::new(GatewayState {
        rate_limiter: RateLimiter::new(config.rate_limit_rps, config.rate_limit_burst),
        client,
        csp,
        upstream_probe: RwLock::new(None),
        config,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_state_returns_config_errors_instead_of_panicking() {
        let config = Config {
            listen_addr: "127.0.0.1:0".parse().unwrap(),
            upstream_api: "http://127.0.0.1:8080".to_string(),
            client_ip_header: None,
            rate_limit_rps: 20.0,
            rate_limit_burst: 40,
            max_body_bytes: 1024,
            upstream_connect_timeout_secs: 5,
            upstream_read_timeout_secs: 30,
            shutdown_grace_secs: 25,
            allowed_origins: vec![],
            csp: None,
            log_format_json: false,
        };
        // build_state surfaces a Result so a reqwest-side rejection becomes a
        // clean boot error (exit 2 with a message), never a mid-boot panic.
        // Zero/negative timeouts are rejected earlier, in Config::from_env.
        let state = build_state(config, "default-src 'self'".into());
        assert!(state.is_ok(), "a valid config must build");
    }
}
