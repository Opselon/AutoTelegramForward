//! Shared handler state: the validated config, one tuned HTTP client, and the
//! rate limiter. Cloned cheaply behind an `Arc` per request.

use std::sync::Arc;
use std::time::Duration;

use crate::config::Config;
use crate::rate_limit::RateLimiter;

pub struct GatewayState {
    pub config: Config,
    pub client: reqwest::Client,
    pub rate_limiter: RateLimiter,
    /// Content-Security-Policy resolved at boot: the operator's explicit value,
    /// or the one derived from the embedded index.html.
    pub csp: String,
}

pub type AppState = Arc<GatewayState>;

/// Build the upstream client. Deliberate choices:
/// - `connect_timeout` bounds the dial only;
/// - `read_timeout` bounds each *idle gap* on the connection, so a slow but
///   live streaming response survives while a hung one is cut — the blanket
///   total `timeout(30 s)` this replaces used to kill long-lived responses;
/// - connection pooling keeps the per-request TLS/handshake cost off the
///   dashboard's 3 s log poll.
pub fn build_client(config: &Config) -> reqwest::Client {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(config.upstream_connect_timeout_secs))
        .read_timeout(Duration::from_secs(config.upstream_read_timeout_secs))
        .pool_idle_timeout(Duration::from_secs(90))
        .pool_max_idle_per_host(32)
        .build()
        .expect("reqwest client builds from valid config")
}

pub fn build_state(config: Config, csp: String) -> AppState {
    Arc::new(GatewayState {
        rate_limiter: RateLimiter::new(config.rate_limit_rps, config.rate_limit_burst),
        client: build_client(&config),
        csp,
        config,
    })
}
