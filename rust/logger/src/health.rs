//! Liveness, readiness, and the emergency diagnostic path.
//!
//! The contract (docs/logging-contract.md §2) requires that a Logger which cannot
//! write its own logs still reports why. Three rules follow from that and are the
//! whole design of this module:
//!
//! 1. **Liveness ≠ readiness.** Liveness answers "is this process alive?" — an
//!    operator's liveness probe must not restart a Logger merely because its
//!    database is momentarily unreachable, because a restart does not fix a
//!    database. Readiness answers "can it fulfil the role it advertises?", so a
//!    process whose storage is broken reports **not ready** with a reason instead
//!    of pretending a `success=true` still means "durable".
//! 2. **The reason is a bounded code, plus human detail.** The code is a fixed
//!    `&'static str` (safe as a metric label), the detail is free text carried in
//!    the health JSON body only.
//! 3. **The emergency path is non-recursive.** [`emergency`] writes one line to
//!    stderr with `eprintln!` — no logging framework, no tracing subscriber, no
//!    storage, no queue, no network, no allocation of a logger. It works before
//!    the runtime starts, after it stops, and while the database is on fire.
//!
//! Readiness is recomputed from live signals (a storage probe and the pipeline
//! state) by the caller; this module only stores the verdict, so the metrics and
//! health listeners can read it without touching anything that can block.

use std::io::Write;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

/// Coarse health state. The label set is closed: safe for metrics.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ready {
    /// Startup is in progress (config parsed, storage not yet proven).
    Starting,
    /// Fully able to fulfil the advertised role.
    Ready,
    /// Serving, but with a weaker guarantee than the operator may assume
    /// (for example `AckMode::Queued`, where `success=true` means "enqueued").
    Degraded,
    /// Graceful shutdown is in progress: new durable work is refused.
    Draining,
    /// Cannot fulfil the advertised role (storage broken, pipeline stopped).
    Down,
}

impl Ready {
    pub fn label(self) -> &'static str {
        match self {
            Ready::Starting => "starting",
            Ready::Ready => "ready",
            Ready::Degraded => "degraded",
            Ready::Draining => "draining",
            Ready::Down => "down",
        }
    }
}

/// Bounded reason codes. Anything exported as a label must come from this list.
pub const REASON_OK: &str = "ok";
pub const REASON_STARTING: &str = "starting";
pub const REASON_STORAGE_UNAVAILABLE: &str = "storage_unavailable";
pub const REASON_MIGRATION_FAILED: &str = "migration_failed";
pub const REASON_SCHEMA_UNUSABLE: &str = "schema_unusable";
pub const REASON_PIPELINE_STOPPED: &str = "pipeline_stopped";
pub const REASON_ACK_MODE_QUEUED: &str = "ack_mode_queued";
pub const REASON_RETENTION_FAILING: &str = "retention_failing";
pub const REASON_DRAINING: &str = "draining";
pub const REASON_SHUTDOWN: &str = "shutdown";
pub const REASON_FATAL: &str = "fatal";

#[derive(Debug)]
struct Inner {
    state: Ready,
    reason: &'static str,
    detail: String,
    /// Unix seconds of the last state change.
    changed_at: i64,
}

/// Shared health state. Cheap to read from any thread; never blocks on I/O.
#[derive(Debug)]
pub struct HealthState {
    alive: AtomicBool,
    ready: AtomicBool,
    inner: Mutex<Inner>,
}

impl Default for HealthState {
    fn default() -> Self {
        Self::new()
    }
}

impl HealthState {
    pub fn new() -> Self {
        HealthState {
            alive: AtomicBool::new(true),
            ready: AtomicBool::new(false),
            inner: Mutex::new(Inner {
                state: Ready::Starting,
                reason: REASON_STARTING,
                detail: "startup in progress".to_string(),
                changed_at: now_secs(),
            }),
        }
    }

    fn set(&self, state: Ready, ready: bool, reason: &'static str, detail: impl Into<String>) {
        self.ready.store(ready, Ordering::Release);
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.state = state;
        inner.reason = reason;
        inner.detail = detail.into();
        inner.changed_at = now_secs();
    }

    /// The process is up, but not yet proven able to serve.
    pub fn mark_starting(&self, detail: impl Into<String>) {
        self.set(Ready::Starting, false, REASON_STARTING, detail);
    }

    /// Fully serving.
    pub fn mark_ready(&self, detail: impl Into<String>) {
        self.set(Ready::Ready, true, REASON_OK, detail);
    }

    /// Serving with a weaker guarantee than the operator might assume.
    pub fn mark_degraded(&self, reason: &'static str, detail: impl Into<String>) {
        self.set(Ready::Degraded, true, reason, detail);
    }

    /// Cannot fulfil the advertised role. `reason` must be a bounded code.
    pub fn mark_not_ready(&self, reason: &'static str, detail: impl Into<String>) {
        self.set(Ready::Down, false, reason, detail);
    }

    /// Graceful drain in progress.
    pub fn mark_draining(&self, detail: impl Into<String>) {
        self.set(Ready::Draining, false, REASON_DRAINING, detail);
    }

    /// The process is stopping (or has hit a fatal error).
    pub fn mark_stopped(&self, detail: impl Into<String>) {
        self.alive.store(false, Ordering::Release);
        self.set(Ready::Down, false, REASON_SHUTDOWN, detail);
    }

    pub fn is_alive(&self) -> bool {
        self.alive.load(Ordering::Acquire)
    }

    pub fn is_ready(&self) -> bool {
        self.ready.load(Ordering::Acquire)
    }

    pub fn state(&self) -> Ready {
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .state
    }

    pub fn state_label(&self) -> &'static str {
        self.state().label()
    }

    pub fn reason_code(&self) -> &'static str {
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .reason
    }

    pub fn detail(&self) -> String {
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .detail
            .clone()
    }

    pub fn changed_at(&self) -> i64 {
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .changed_at
    }

    /// One-line summary for the startup diagnostic.
    pub fn describe(&self) -> String {
        format!(
            "state={} reason={} ready={} alive={} detail={}",
            self.state_label(),
            self.reason_code(),
            self.is_ready(),
            self.is_alive(),
            self.detail()
        )
    }
}

fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Write one emergency diagnostic line to stderr.
///
/// This is the *only* path startup/fatal diagnostics use. It deliberately bypasses
/// `tracing`, the metrics registry, the storage backend and the pipeline, so a
/// Logger that cannot write its own logs still says why. `eprintln!` locks stderr
/// per call, so lines from different threads cannot interleave mid-line.
pub fn emergency(event: &str, detail: &str) {
    let _ = writeln!(
        std::io::stderr(),
        "atf-logger EMERGENCY ts={} event={} detail={}",
        now_secs(),
        sanitize(event),
        sanitize(detail)
    );
}

/// Routine progress line to stderr (retention cycles, drains, migrations).
///
/// Same non-recursive path as [`emergency`] on purpose: stdout belongs to the
/// harness handshake and must not carry a single other byte, so *every* diagnostic
/// this binary emits — routine or fatal — goes to stderr through this module.
pub fn note(event: &str, detail: &str) {
    let _ = writeln!(
        std::io::stderr(),
        "atf-logger NOTE ts={} event={} detail={}",
        now_secs(),
        sanitize(event),
        sanitize(detail)
    );
}

/// Emergency line with structured, bounded fields (`key=value`, newlines folded).
pub fn emergency_fields(event: &str, fields: &[(&str, &str)]) {
    let mut line = format!("atf-logger EMERGENCY ts={} event={}", now_secs(), sanitize(event));
    for (key, value) in fields {
        line.push(' ');
        line.push_str(&sanitize(key));
        line.push('=');
        line.push_str(&sanitize(value));
    }
    let _ = writeln!(std::io::stderr(), "{line}");
}

/// Fold newlines/tabs so one diagnostic can never forge a second line, and bound
/// the length so a pathological detail cannot flood the log.
fn sanitize(value: &str) -> String {
    const LIMIT: usize = 2048;
    let mut out = String::with_capacity(value.len().min(LIMIT));
    for ch in value.chars() {
        if out.len() >= LIMIT {
            out.push_str("…[truncated]");
            break;
        }
        match ch {
            '\n' | '\r' => out.push(' '),
            '\t' => out.push(' '),
            c => out.push(c),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn liveness_and_readiness_are_independent() {
        let health = HealthState::new();
        // A starting process is alive but not ready.
        assert!(health.is_alive());
        assert!(!health.is_ready());
        assert_eq!(health.state_label(), "starting");
        assert_eq!(health.reason_code(), REASON_STARTING);

        // A broken database: alive, explicitly not ready, with a reason.
        health.mark_not_ready(REASON_STORAGE_UNAVAILABLE, "postgres connect refused");
        assert!(health.is_alive());
        assert!(!health.is_ready());
        assert_eq!(health.state_label(), "down");
        assert_eq!(health.reason_code(), "storage_unavailable");
        assert!(health.detail().contains("connect refused"));

        // Recovered.
        health.mark_ready("storage probe ok");
        assert!(health.is_ready());
        assert_eq!(health.state_label(), "ready");
        assert_eq!(health.reason_code(), REASON_OK);

        // Draining is not ready (new durable work must be refused), still alive.
        health.mark_draining("drain started");
        assert!(health.is_alive());
        assert!(!health.is_ready());

        // Fatal/stopped.
        health.mark_stopped("fatal: migration failed");
        assert!(!health.is_alive());
        assert!(!health.is_ready());
        assert_eq!(health.reason_code(), "shutdown");
    }

    #[test]
    fn queued_ack_mode_is_degraded_not_ready_for_durability() {
        let health = HealthState::new();
        health.mark_degraded(REASON_ACK_MODE_QUEUED, "ack-mode=queued: success means enqueued");
        assert!(health.is_ready(), "queued mode still serves requests");
        assert_eq!(health.state_label(), "degraded");
        assert_eq!(health.reason_code(), "ack_mode_queued");
    }

    #[test]
    fn sanitize_folds_newlines_and_bounds_length() {
        assert_eq!(sanitize("a\nb\tc"), "a b c");
        let long = "x".repeat(10_000);
        let folded = sanitize(&long);
        assert!(folded.len() < 2_200, "{}", folded.len());
        assert!(folded.ends_with("[truncated]"));
    }

    #[test]
    fn emergency_does_not_panic_without_a_logger() {
        // No subscriber installed, no runtime, no storage: still safe.
        emergency("test_event", "detail\nwith newline");
        emergency_fields("test_fields", &[("backend", "sqlite"), ("path", "/tmp/x.db")]);
        assert!(HealthState::new().describe().contains("state=starting"));
    }
}
