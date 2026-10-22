//! Validated runtime configuration.
//!
//! Sources, in priority order: command-line flag, environment variable,
//! built-in default. The defaults are chosen so that "just run the binary" is
//! safe: an operator who configures nothing gets a loopback metrics port, the
//! Python engine's database path and port, durable acknowledgements and a
//! bounded queue.
//!
//! Everything is validated up front — a bad combination fails startup with a
//! `ConfigError` instead of misbehaving hours later.

use std::fmt;

/// How `Log` acknowledges a record.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AckMode {
    /// `success = true` only after the batch containing the record committed to
    /// SQLite (WAL). The strongest guarantee; the ack latency includes at most
    /// one batch delay. This is the default.
    Durable,
    /// `success = true` once the record is in the bounded in-process queue.
    /// Cheaper and lower latency, but the ack means "admitted", not "on disk".
    Queued,
}

impl AckMode {
    pub fn as_str(self) -> &'static str {
        match self {
            AckMode::Durable => "durable",
            AckMode::Queued => "queued",
        }
    }
}

/// Unit of the caller-supplied `ts` column, used only by the retention worker.
///
/// `LogControlService.LogEntry.ts` is an opaque `int64` the producer fills in
/// (the Python client writes seconds, `scripts/bench/logger_baseline.py` writes
/// milliseconds) and the engine stores it verbatim — exactly like the Python
/// engine. Age-based retention therefore needs to be told which unit the
/// archive is in, because "now" has to be expressed in the same unit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TsUnit {
    Seconds,
    Millis,
}

impl TsUnit {
    pub fn as_str(self) -> &'static str {
        match self {
            TsUnit::Seconds => "s",
            TsUnit::Millis => "ms",
        }
    }

    /// `now` in this unit, for building age cutoffs.
    pub fn now(self) -> i64 {
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        match self {
            TsUnit::Seconds => secs,
            TsUnit::Millis => secs.saturating_mul(1000),
        }
    }

    pub fn hours_in_unit(self, hours: u64) -> i64 {
        let secs = (hours as i64).saturating_mul(3600);
        match self {
            TsUnit::Seconds => secs,
            TsUnit::Millis => secs.saturating_mul(1000),
        }
    }
}

/// A validated configuration error. Carries the offending key so an operator
/// can fix it without reading the source.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError {
    pub key: String,
    pub message: String,
}

impl ConfigError {
    fn new(key: &str, message: impl Into<String>) -> Self {
        ConfigError {
            key: key.to_string(),
            message: message.into(),
        }
    }
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid configuration: {}: {}", self.key, self.message)
    }
}

impl std::error::Error for ConfigError {}

/// Per-severity / per-category retention override.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetentionRule {
    pub selector: String,
    pub max_age_hours: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    pub db_path: String,
    pub listen: String,
    pub metrics_listen: String,
    pub print_addr: bool,

    pub queue_capacity: usize,
    pub priority_capacity: usize,
    pub batch_size: usize,
    pub batch_delay_ms: u64,
    pub poll_interval_ms: u64,

    pub ack_mode: AckMode,
    pub ack_timeout_ms: u64,

    pub busy_timeout_ms: u64,
    pub read_pool_size: usize,
    pub max_retries: u32,
    pub retry_base_ms: u64,
    pub drain_timeout_ms: u64,
    pub purge_timeout_ms: u64,

    pub max_message_bytes: usize,
    pub max_detail_bytes: usize,
    pub max_tag_bytes: usize,

    /// Capability-scoped bearer tokens for the gRPC surface. Empty string =
    /// scope open (previous behaviour; documented trade-off in the audit):
    /// producers keep logging through an unattended rotation window.
    pub auth_ingest_token: String,
    pub auth_read_token: String,
    pub auth_admin_token: String,

    pub redact_identifiers: bool,
    pub dedupe_window_secs: u64,

    pub retention_enabled: bool,
    pub retention_interval_secs: u64,
    pub retention_default_max_age_hours: u64,
    pub retention_level_hours: Vec<RetentionRule>,
    pub retention_category_hours: Vec<RetentionRule>,
    pub protected_categories: Vec<String>,
    pub retention_ts_unit: TsUnit,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            db_path: "data/atf_logs.db".to_string(),
            listen: "0.0.0.0:50052".to_string(),
            metrics_listen: "127.0.0.1:9109".to_string(),
            print_addr: false,
            queue_capacity: 8192,
            priority_capacity: 2048,
            batch_size: 512,
            batch_delay_ms: 2,
            poll_interval_ms: 25,
            ack_mode: AckMode::Durable,
            ack_timeout_ms: 10_000,
            busy_timeout_ms: 5_000,
            read_pool_size: 2,
            max_retries: 5,
            retry_base_ms: 20,
            drain_timeout_ms: 15_000,
            purge_timeout_ms: 120_000,
            max_message_bytes: 1 << 20,
            max_detail_bytes: 1 << 20,
            max_tag_bytes: 4096,
            auth_ingest_token: String::new(),
            auth_read_token: String::new(),
            auth_admin_token: String::new(),
            redact_identifiers: true,
            dedupe_window_secs: 0,
            retention_enabled: true,
            retention_interval_secs: 300,
            retention_default_max_age_hours: 24 * 7,
            retention_level_hours: Vec::new(),
            retention_category_hours: Vec::new(),
            protected_categories: vec!["audit".to_string(), "security".to_string()],
            retention_ts_unit: TsUnit::Seconds,
        }
    }
}

/// The normal-lane capacity: whatever is left of the total budget after the
/// reserved high-severity capacity.
pub fn normal_lane_capacity(total: usize, priority: usize) -> usize {
    total.saturating_sub(priority).max(1)
}

impl Config {
    /// Parse `args` (without the program name) and `env`.
    pub fn from_parts(
        args: &[String],
        env: &dyn Fn(&str) -> Option<String>,
    ) -> Result<Self, ConfigError> {
        let mut cfg = Config::default();
        cfg.apply_env(env)?;
        let mut i = 0;
        while i < args.len() {
            let flag = args[i].as_str();
            let take = |i: &mut usize| -> Result<String, ConfigError> {
                *i += 1;
                args.get(*i).cloned().ok_or_else(|| {
                    ConfigError::new(flag, "flag requires a value (e.g. --db /data/atf_logs.db)")
                })
            };
            match flag {
                "--db" => cfg.db_path = take(&mut i)?,
                "--listen" => cfg.listen = take(&mut i)?,
                "--metrics-listen" => cfg.metrics_listen = take(&mut i)?,
                "--print-addr" => cfg.print_addr = true,
                "--queue-capacity" => cfg.queue_capacity = parse_usize(flag, &take(&mut i)?)?,
                "--priority-capacity" => cfg.priority_capacity = parse_usize(flag, &take(&mut i)?)?,
                "--batch-size" => cfg.batch_size = parse_usize(flag, &take(&mut i)?)?,
                "--batch-delay-ms" => cfg.batch_delay_ms = parse_u64(flag, &take(&mut i)?)?,
                "--poll-interval-ms" => cfg.poll_interval_ms = parse_u64(flag, &take(&mut i)?)?,
                "--ack-mode" => {
                    let v = take(&mut i)?;
                    cfg.ack_mode = match v.as_str() {
                        "durable" => AckMode::Durable,
                        "queued" => AckMode::Queued,
                        other => {
                            return Err(ConfigError::new(
                                flag,
                                format!("expected durable|queued, got {other}"),
                            ))
                        }
                    };
                }
                "--ack-timeout-ms" => cfg.ack_timeout_ms = parse_u64(flag, &take(&mut i)?)?,
                "--busy-timeout-ms" => cfg.busy_timeout_ms = parse_u64(flag, &take(&mut i)?)?,
                "--read-pool-size" => cfg.read_pool_size = parse_usize(flag, &take(&mut i)?)?,
                "--max-retries" => cfg.max_retries = parse_usize(flag, &take(&mut i)?)? as u32,
                "--retry-base-ms" => cfg.retry_base_ms = parse_u64(flag, &take(&mut i)?)?,
                "--drain-timeout-ms" => cfg.drain_timeout_ms = parse_u64(flag, &take(&mut i)?)?,
                "--purge-timeout-ms" => cfg.purge_timeout_ms = parse_u64(flag, &take(&mut i)?)?,
                "--max-message-bytes" => cfg.max_message_bytes = parse_usize(flag, &take(&mut i)?)?,
                "--max-detail-bytes" => cfg.max_detail_bytes = parse_usize(flag, &take(&mut i)?)?,
                "--max-tag-bytes" => cfg.max_tag_bytes = parse_usize(flag, &take(&mut i)?)?,
                "--redact-identifiers" => cfg.redact_identifiers = true,
                "--no-redact-identifiers" => cfg.redact_identifiers = false,
                "--dedupe-window-secs" => cfg.dedupe_window_secs = parse_u64(flag, &take(&mut i)?)?,
                "--retention" => cfg.retention_enabled = true,
                "--no-retention" => cfg.retention_enabled = false,
                "--retention-interval-secs" => {
                    cfg.retention_interval_secs = parse_u64(flag, &take(&mut i)?)?
                }
                "--retention-max-age-hours" => {
                    cfg.retention_default_max_age_hours = parse_u64(flag, &take(&mut i)?)?
                }
                "--retention-level" => {
                    let v = take(&mut i)?;
                    cfg.retention_level_hours.push(parse_rule(flag, &v)?);
                }
                "--retention-category" => {
                    let v = take(&mut i)?;
                    cfg.retention_category_hours.push(parse_rule(flag, &v)?);
                }
                "--protect-category" => cfg.protected_categories.push(take(&mut i)?.to_uppercase()),
                "--retention-ts-unit" => {
                    let v = take(&mut i)?;
                    cfg.retention_ts_unit = match v.as_str() {
                        "s" | "seconds" => TsUnit::Seconds,
                        "ms" | "millis" | "milliseconds" => TsUnit::Millis,
                        other => {
                            return Err(ConfigError::new(
                                flag,
                                format!("expected s|ms, got {other}"),
                            ))
                        }
                    };
                }
                "--help" | "-h" => return Err(ConfigError::new("--help", HELP)),
                "--version" | "-V" => {
                    return Err(ConfigError::new("--version", crate::VERSION.to_string()))
                }
                other => {
                    return Err(ConfigError::new(
                        other,
                        "unknown flag (see --help for the supported set)",
                    ))
                }
            }
            i += 1;
        }
        cfg.validate()?;
        Ok(cfg)
    }

    /// Parse the process arguments and environment.
    pub fn from_env() -> Result<Self, ConfigError> {
        let args: Vec<String> = std::env::args().skip(1).collect();
        Self::from_parts(&args, &|k| std::env::var(k).ok())
    }

    fn apply_env(&mut self, env: &dyn Fn(&str) -> Option<String>) -> Result<(), ConfigError> {
        let set_str = |key: &str, slot: &mut String| {
            if let Some(v) = env(key) {
                if !v.is_empty() {
                    *slot = v;
                }
            }
        };
        set_str("ATF_LOGGER_DB_PATH", &mut self.db_path);
        set_str("ATF_LOGGER_DB", &mut self.db_path);
        set_str("ATF_LOGGER_LISTEN", &mut self.listen);
        // Backwards compatibility with logger/main.py's HOST/PORT pair.
        if env("ATF_LOGGER_LISTEN").is_none() {
            let host = env("ATF_LOGGER_HOST").unwrap_or_default();
            let port = env("ATF_LOGGER_PORT").unwrap_or_default();
            if !host.is_empty() || !port.is_empty() {
                self.listen = format!(
                    "{}:{}",
                    if host.is_empty() { "0.0.0.0" } else { &host },
                    if port.is_empty() { "50052" } else { &port }
                );
            }
        }
        set_str("ATF_LOGGER_METRICS_LISTEN", &mut self.metrics_listen);

        set_str("ATF_LOGGER_INGEST_TOKEN", &mut self.auth_ingest_token);
        set_str("ATF_LOGGER_READ_TOKEN", &mut self.auth_read_token);
        set_str("ATF_LOGGER_ADMIN_TOKEN", &mut self.auth_admin_token);

        macro_rules! from_env {
            ($key:expr, $slot:expr, $parse:expr) => {
                if let Some(v) = env($key) {
                    if !v.is_empty() {
                        $slot = $parse($key, &v)?;
                    }
                }
            };
        }
        from_env!(
            "ATF_LOGGER_QUEUE_CAPACITY",
            self.queue_capacity,
            parse_usize
        );
        from_env!(
            "ATF_LOGGER_PRIORITY_CAPACITY",
            self.priority_capacity,
            parse_usize
        );
        from_env!("ATF_LOGGER_BATCH_SIZE", self.batch_size, parse_usize);
        from_env!("ATF_LOGGER_BATCH_DELAY_MS", self.batch_delay_ms, parse_u64);
        from_env!(
            "ATF_LOGGER_POLL_INTERVAL_MS",
            self.poll_interval_ms,
            parse_u64
        );
        from_env!("ATF_LOGGER_ACK_TIMEOUT_MS", self.ack_timeout_ms, parse_u64);
        from_env!(
            "ATF_LOGGER_BUSY_TIMEOUT_MS",
            self.busy_timeout_ms,
            parse_u64
        );
        from_env!(
            "ATF_LOGGER_READ_POOL_SIZE",
            self.read_pool_size,
            parse_usize
        );
        from_env!("ATF_LOGGER_MAX_RETRIES", self.max_retries, |k, v| {
            parse_usize(k, v).map(|n| n as u32)
        });
        from_env!("ATF_LOGGER_RETRY_BASE_MS", self.retry_base_ms, parse_u64);
        from_env!(
            "ATF_LOGGER_DRAIN_TIMEOUT_MS",
            self.drain_timeout_ms,
            parse_u64
        );
        from_env!(
            "ATF_LOGGER_DEDUPE_WINDOW_SECS",
            self.dedupe_window_secs,
            parse_u64
        );
        from_env!(
            "ATF_LOGGER_RETENTION_INTERVAL_SECS",
            self.retention_interval_secs,
            parse_u64
        );
        from_env!(
            "ATF_LOGGER_RETENTION_MAX_AGE_HOURS",
            self.retention_default_max_age_hours,
            parse_u64
        );
        if let Some(v) = env("ATF_LOGGER_ACK_MODE") {
            if !v.is_empty() {
                self.ack_mode = match v.as_str() {
                    "durable" => AckMode::Durable,
                    "queued" => AckMode::Queued,
                    other => {
                        return Err(ConfigError::new(
                            "ATF_LOGGER_ACK_MODE",
                            format!("expected durable|queued, got {other}"),
                        ))
                    }
                };
            }
        }
        if let Some(v) = env("ATF_LOGGER_RETENTION") {
            self.retention_enabled = v != "0" && !v.eq_ignore_ascii_case("false");
        }
        if let Some(v) = env("ATF_LOGGER_REDACT_IDENTIFIERS") {
            self.redact_identifiers = v != "0" && !v.eq_ignore_ascii_case("false");
        }
        Ok(())
    }

    /// Reject combinations that would make the service misbehave.
    pub fn validate(&self) -> Result<(), ConfigError> {
        if self.db_path.trim().is_empty() {
            return Err(ConfigError::new("--db", "database path must not be empty"));
        }
        if self.db_path == ":memory:" {
            return Err(ConfigError::new(
                "--db",
                ":memory: is single-connection only; use a file path (WAL)",
            ));
        }
        if !self.listen.contains(':') {
            return Err(ConfigError::new("--listen", "expected host:port"));
        }
        if !self.metrics_listen.contains(':') {
            return Err(ConfigError::new("--metrics-listen", "expected host:port"));
        }
        if self.metrics_listen == self.listen {
            return Err(ConfigError::new(
                "--metrics-listen",
                "metrics must not share the gRPC port",
            ));
        }
        if self.queue_capacity == 0 {
            return Err(ConfigError::new("--queue-capacity", "must be > 0"));
        }
        if self.priority_capacity == 0 {
            return Err(ConfigError::new("--priority-capacity", "must be > 0"));
        }
        if self.priority_capacity >= self.queue_capacity {
            return Err(ConfigError::new(
                "--priority-capacity",
                format!(
                    "must be smaller than --queue-capacity ({} >= {})",
                    self.priority_capacity, self.queue_capacity
                ),
            ));
        }
        if self.batch_size == 0 {
            return Err(ConfigError::new("--batch-size", "must be > 0"));
        }
        if self.batch_size > 100_000 {
            return Err(ConfigError::new("--batch-size", "must be <= 100000"));
        }
        if self.batch_delay_ms == 0 || self.batch_delay_ms > 60_000 {
            return Err(ConfigError::new("--batch-delay-ms", "must be in 1..=60000"));
        }
        if self.poll_interval_ms == 0 || self.poll_interval_ms > 5_000 {
            return Err(ConfigError::new(
                "--poll-interval-ms",
                "must be in 1..=5000",
            ));
        }
        if self.ack_timeout_ms == 0 {
            return Err(ConfigError::new("--ack-timeout-ms", "must be > 0"));
        }
        if self.drain_timeout_ms == 0 {
            return Err(ConfigError::new("--drain-timeout-ms", "must be > 0"));
        }
        if self.read_pool_size == 0 || self.read_pool_size > 16 {
            return Err(ConfigError::new("--read-pool-size", "must be in 1..=16"));
        }
        if self.max_message_bytes == 0 || self.max_detail_bytes == 0 || self.max_tag_bytes == 0 {
            return Err(ConfigError::new(
                "--max-message-bytes",
                "size limits must be > 0",
            ));
        }
        if self.dedupe_window_secs > 3_600 {
            return Err(ConfigError::new(
                "--dedupe-window-secs",
                "must be <= 3600 (a longer window is a memory leak, not dedupe)",
            ));
        }
        if self.retention_enabled && self.retention_interval_secs == 0 {
            return Err(ConfigError::new(
                "--retention-interval-secs",
                "must be > 0 when retention is enabled",
            ));
        }
        for rule in self
            .retention_level_hours
            .iter()
            .chain(self.retention_category_hours.iter())
        {
            if rule.max_age_hours == 0 {
                return Err(ConfigError::new(
                    &rule.selector,
                    "retention age must be > 0 hours (0 would purge everything)",
                ));
            }
        }
        Ok(())
    }

    /// Stable short fingerprint of the effective configuration, exported as the
    /// `atf_logger_config_version` label so an operator can tell two differently
    /// configured pods apart without reading their flags.
    pub fn version_hash(&self) -> String {
        let mut h: u64 = 0xcbf2_9ce4_8422_2325;
        let mut feed = |s: &str| {
            for b in s.as_bytes() {
                h ^= u64::from(*b);
                h = h.wrapping_mul(0x0000_0100_0000_01b3);
            }
            h ^= 0x1f;
            h = h.wrapping_mul(0x0000_0100_0000_01b3);
        };
        feed(self.db_path.as_str());
        feed(self.listen.as_str());
        feed(self.metrics_listen.as_str());
        feed(self.ack_mode.as_str());
        feed(&self.queue_capacity.to_string());
        feed(&self.priority_capacity.to_string());
        feed(&self.batch_size.to_string());
        feed(&self.batch_delay_ms.to_string());
        feed(&self.read_pool_size.to_string());
        feed(&self.busy_timeout_ms.to_string());
        feed(&self.redact_identifiers.to_string());
        feed(&self.dedupe_window_secs.to_string());
        feed(&self.retention_enabled.to_string());
        feed(&self.retention_default_max_age_hours.to_string());
        feed(self.retention_ts_unit.as_str());
        for c in &self.protected_categories {
            feed(c);
        }
        // Auth tokens change the config fingerprint too, but only their
        // presence ever enters the hash: the hash is rendered by /metrics and
        // must never echo a secret.
        feed(derive_auth_state(self).as_str());
        format!("{h:016x}")
    }

    /// True when `category` is protected from every age-based purge.
    pub fn is_protected_category(&self, category: &str) -> bool {
        let c = category.to_uppercase();
        self.protected_categories.iter().any(|p| p == &c)
    }

    pub fn is_high_severity(&self, level: &str) -> bool {
        matches!(level, "ERROR" | "CRITICAL")
    }
}

fn parse_usize(key: &str, v: &str) -> Result<usize, ConfigError> {
    v.parse::<usize>()
        .map_err(|_| ConfigError::new(key, format!("expected a non-negative integer, got {v}")))
}

fn parse_u64(key: &str, v: &str) -> Result<u64, ConfigError> {
    v.parse::<u64>()
        .map_err(|_| ConfigError::new(key, format!("expected a non-negative integer, got {v}")))
}

fn parse_rule(key: &str, v: &str) -> Result<RetentionRule, ConfigError> {
    let (name, hours) = v
        .split_once('=')
        .ok_or_else(|| ConfigError::new(key, format!("expected NAME=HOURS, got {v}")))?;
    if name.trim().is_empty() {
        return Err(ConfigError::new(key, "selector must not be empty"));
    }
    let hours = parse_u64(key, hours.trim())?;
    Ok(RetentionRule {
        selector: name.trim().to_string(),
        max_age_hours: hours,
    })
}

pub const HELP: &str = "\
atf-logger — bounded, batched gRPC log ingest/storage/query engine

USAGE: atf-logger [OPTIONS]

  --db <PATH>                  SQLite archive (env ATF_LOGGER_DB_PATH)
  --listen <ADDR>              gRPC bind address (env ATF_LOGGER_LISTEN)
  --metrics-listen <ADDR>      Prometheus/health bind address (env ATF_LOGGER_METRICS_LISTEN)
  --print-addr                 print `listening on <ADDR>` as the first stdout line
  --queue-capacity <N>         total bounded queue slots
  --priority-capacity <N>      slots reserved for ERROR/CRITICAL
  --batch-size <N>             records per SQLite transaction
  --batch-delay-ms <N>         max time a batch waits before it is flushed
  --ack-mode <durable|queued>  what success=true means on Log
  --ack-timeout-ms <N>         deadline for a durable ack
  --busy-timeout-ms <N>        SQLite busy_timeout
  --read-pool-size <N>         read-only SQLite connections
  --max-retries <N>            bounded write retries before a batch is failed
  --drain-timeout-ms <N>       graceful shutdown budget
  --purge-timeout-ms <N>       deadline for a PurgeLogs RPC
  --dedupe-window-secs <N>     0 disables duplicate suppression
  --redact-identifiers         redact rule_id/correlation_id too (default on)
  --retention / --no-retention enable or disable the retention worker
  --retention-interval-secs <N>
  --retention-max-age-hours <N>
  --retention-level NAME=HOURS
  --retention-category NAME=HOURS
  --protect-category NAME      never age-purged (repeatable)
  --retention-ts-unit <s|ms>   unit of the stored ts column
  --version, --help
";

/// Bounded auth-state fingerprint for `version_hash` (presence only).
fn derive_auth_state(c: &Config) -> String {
    let on = [
        !c.auth_ingest_token.is_empty(),
        !c.auth_read_token.is_empty(),
        !c.auth_admin_token.is_empty(),
    ];
    let mut bits = String::new();
    for b in on {
        bits.push(if b { '1' } else { '0' });
    }
    bits
}

#[cfg(test)]
mod tests {
    use super::*;

    fn no_env(_: &str) -> Option<String> {
        None
    }

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| (*s).to_string()).collect()
    }

    #[test]
    fn defaults_are_valid() {
        let cfg = Config::default();
        cfg.validate().expect("defaults must be valid");
        assert_eq!(cfg.db_path, "data/atf_logs.db");
        assert_eq!(cfg.listen, "0.0.0.0:50052");
        assert_eq!(cfg.ack_mode, AckMode::Durable);
        assert_eq!(cfg.metrics_listen, "127.0.0.1:9109");
    }

    #[test]
    fn parses_flags() {
        let cfg = Config::from_parts(
            &args(&[
                "--db",
                "/tmp/x.db",
                "--listen",
                "127.0.0.1:0",
                "--batch-size",
                "64",
                "--ack-mode",
                "queued",
                "--print-addr",
            ]),
            &no_env,
        )
        .unwrap();
        assert_eq!(cfg.db_path, "/tmp/x.db");
        assert_eq!(cfg.batch_size, 64);
        assert_eq!(cfg.ack_mode, AckMode::Queued);
        assert!(cfg.print_addr);
    }

    #[test]
    fn env_wins_over_default_and_flags_win_over_env() {
        let env = |k: &str| match k {
            "ATF_LOGGER_DB_PATH" => Some("/srv/logs.db".to_string()),
            "ATF_LOGGER_BATCH_SIZE" => Some("16".to_string()),
            _ => None,
        };
        let cfg = Config::from_parts(&args(&["--db", "/tmp/override.db"]), &env).unwrap();
        assert_eq!(cfg.db_path, "/tmp/override.db");
        assert_eq!(cfg.batch_size, 16);
    }

    #[test]
    fn honours_legacy_host_port_env_pair() {
        let env = |k: &str| match k {
            "ATF_LOGGER_HOST" => Some("127.0.0.1".to_string()),
            "ATF_LOGGER_PORT" => Some("50099".to_string()),
            _ => None,
        };
        let cfg = Config::from_parts(&args(&[]), &env).unwrap();
        assert_eq!(cfg.listen, "127.0.0.1:50099");
    }

    #[test]
    fn rejects_metrics_on_grpc_port() {
        let err = Config::from_parts(
            &args(&[
                "--listen",
                "0.0.0.0:50052",
                "--metrics-listen",
                "0.0.0.0:50052",
            ]),
            &no_env,
        )
        .unwrap_err();
        assert_eq!(err.key, "--metrics-listen");
    }

    #[test]
    fn rejects_priority_lane_larger_than_queue() {
        let err = Config::from_parts(
            &args(&["--queue-capacity", "100", "--priority-capacity", "100"]),
            &no_env,
        )
        .unwrap_err();
        assert_eq!(err.key, "--priority-capacity");
    }

    #[test]
    fn rejects_zero_batch_and_unknown_flags() {
        assert_eq!(
            Config::from_parts(&args(&["--batch-size", "0"]), &no_env)
                .unwrap_err()
                .key,
            "--batch-size"
        );
        assert_eq!(
            Config::from_parts(&args(&["--nope"]), &no_env)
                .unwrap_err()
                .key,
            "--nope"
        );
    }

    #[test]
    fn rejects_missing_flag_value() {
        assert_eq!(
            Config::from_parts(&args(&["--db"]), &no_env)
                .unwrap_err()
                .key,
            "--db"
        );
    }

    #[test]
    fn rejects_memory_database() {
        assert!(Config::from_parts(&args(&["--db", ":memory:"]), &no_env).is_err());
    }

    #[test]
    fn parses_retention_rules() {
        let cfg = Config::from_parts(
            &args(&[
                "--retention-level",
                "DEBUG=24",
                "--retention-category",
                "system=168",
                "--protect-category",
                "audit",
                "--retention-ts-unit",
                "ms",
            ]),
            &no_env,
        )
        .unwrap();
        assert_eq!(cfg.retention_level_hours[0].selector, "DEBUG");
        assert_eq!(cfg.retention_level_hours[0].max_age_hours, 24);
        assert_eq!(cfg.retention_category_hours[0].selector, "system");
        assert_eq!(cfg.retention_ts_unit, TsUnit::Millis);
        assert!(cfg.protected_categories.contains(&"AUDIT".to_string()));
    }

    #[test]
    fn rejects_malformed_retention_rule() {
        assert!(Config::from_parts(&args(&["--retention-level", "DEBUG"]), &no_env).is_err());
        assert!(Config::from_parts(&args(&["--retention-level", "DEBUG=0"]), &no_env).is_err());
    }

    #[test]
    fn version_hash_is_stable_and_sensitive() {
        let a = Config::default();
        let mut b = Config::default();
        assert_eq!(a.version_hash(), b.version_hash());
        b.batch_size += 1;
        assert_ne!(a.version_hash(), b.version_hash());
    }

    #[test]
    fn auth_tokens_default_open_and_parse_from_env() {
        let cfg = Config::default();
        assert!(cfg.auth_ingest_token.is_empty());
        let env = |key: &str| -> Option<String> {
            match key {
                "ATF_LOGGER_INGEST_TOKEN" => Some("s3cr3t-ingest".to_string()),
                "ATF_LOGGER_READ_TOKEN" => Some("s3cr3t-read".to_string()),
                "ATF_LOGGER_ADMIN_TOKEN" => Some("s3cr3t-admin".to_string()),
                _ => None,
            }
        };
        let cfg = Config::from_parts(&[], &env).expect("valid config");
        assert_eq!(cfg.auth_ingest_token, "s3cr3t-ingest");
        assert_eq!(cfg.auth_read_token, "s3cr3t-read");
        assert_eq!(cfg.auth_admin_token, "s3cr3t-admin");
        // An explicitly empty secret leaves the scope open, it must not become
        // an unlockable empty credential.
        let env = |key: &str| -> Option<String> {
            if key == "ATF_LOGGER_INGEST_TOKEN" {
                Some(String::new())
            } else {
                None
            }
        };
        let cfg = Config::from_parts(&[], &env).expect("valid config");
        assert!(cfg.auth_ingest_token.is_empty());
    }

    #[test]
    fn version_hash_never_receives_the_secret_itself() {
        let cfg = Config {
            auth_ingest_token: "topsecret-abc123".to_string(),
            ..Config::default()
        };
        let hash = cfg.version_hash();
        assert!(!hash.contains("topsecret"), "hash must not echo the secret");
        // …but arming a gate does change the fingerprint.
        let open = Config::default(); // auth_ingest_token is empty
        assert_ne!(hash, open.version_hash());
    }

    #[test]
    fn capacity_split() {
        assert_eq!(normal_lane_capacity(8192, 2048), 6144);
        assert_eq!(normal_lane_capacity(4, 4), 1);
        assert_eq!(normal_lane_capacity(1, 8), 1);
    }

    #[test]
    fn ts_unit_math() {
        assert_eq!(TsUnit::Seconds.hours_in_unit(2), 7200);
        assert_eq!(TsUnit::Millis.hours_in_unit(2), 7_200_000);
        assert!(TsUnit::Millis.now() > 1_600_000_000_000);
    }
}
