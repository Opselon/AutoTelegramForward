//! Prometheus text-format registry and the metrics/health HTTP listener.
//!
//! Design rules taken from the mission spec:
//!
//! * **Bounded label cardinality.** Nothing here accepts a user id, message id,
//!   rule id, correlation id or free-form service name as a label. Levels pass
//!   through [`level_label`], which maps everything outside the canonical set to
//!   `other`, and every labelled family is capped by [`MAX_SERIES`] so a producer
//!   cannot inflate the registry even if a future call site forgets a bound.
//! * **Explicit loss accounting.** `received`, `accepted`, `rejected`,
//!   `filtered`, `persisted`, `dropped`, `retried` and `duplicate` are separate
//!   counters. A record that is not persisted is always visible in at least one
//!   of them; there is no "other" bucket that hides loss.
//! * **Nothing recursive.** The exposition handler reads atomics and a shared
//!   health state; it never touches storage, the pipeline or the logger's own
//!   diagnostics, so it still answers when storage is broken — which is exactly
//!   when an operator needs it.
//! * **No allocation on the hot path beyond the label lookup.** Counters are
//!   atomics; histograms are fixed-size arrays of atomics.
//!
//! The main listener serves `/metrics` (Prometheus text), `/healthz` (liveness)
//! and `/readyz` (readiness with a reason), and returns 404 for anything else.

use crate::health::HealthState;
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Hard cap on the number of series any one labelled family may hold.
pub const MAX_SERIES: usize = 64;

/// The series excess label values are folded into. It is a real series, not a gap:
/// the folded counts are still exported, so nothing disappears from the registry.
pub const OVERFLOW_LABEL: &str = "other";

/// Insert-or-get one series under a hard bound.
///
/// The overflow series occupies one of the [`MAX_SERIES`] slots, so the family can
/// never hold — or render — more series than the cap. The previous shape of this
/// guard checked the cap *before* creating the overflow series, so a family at the
/// cap produced `MAX_SERIES + 1` series and the bound it advertised was off by one.
fn bounded_series<T: Clone>(
    series: &mut BTreeMap<String, T>,
    value: &str,
    create: impl FnOnce() -> T,
) -> T {
    if let Some(existing) = series.get(value) {
        return existing.clone();
    }
    // Reserve the last slot for the overflow series: `len + 1` is what the family
    // would hold after adding one more named value.
    if series.len() + 1 >= MAX_SERIES || value == OVERFLOW_LABEL {
        return series
            .entry(OVERFLOW_LABEL.to_string())
            .or_insert_with(create)
            .clone();
    }
    let item = create();
    series.insert(value.to_string(), item.clone());
    item
}

/// Latency buckets in seconds; the same set Prometheus' own client libraries
/// default to, which keeps dashboards portable.
pub const LATENCY_BUCKETS: &[f64] = &[
    0.0001, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
];

/// Batch-size buckets (rows per flush).
pub const BATCH_BUCKETS: &[f64] = &[
    1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 1024.0, 4096.0,
];

/// A monotonically increasing counter.
#[derive(Debug, Default)]
pub struct Counter {
    value: AtomicU64,
}

impl Counter {
    pub fn inc(&self) {
        self.add(1);
    }

    pub fn add(&self, delta: u64) {
        self.value.fetch_add(delta, Ordering::Relaxed);
    }

    pub fn get(&self) -> u64 {
        self.value.load(Ordering::Relaxed)
    }
}

/// A value that goes up and down (queue depth, pool utilisation, in-flight RPCs).
#[derive(Debug, Default)]
pub struct Gauge {
    value: AtomicI64,
}

impl Gauge {
    pub fn set(&self, value: i64) {
        self.value.store(value, Ordering::Relaxed);
    }

    pub fn inc(&self) {
        self.value.fetch_add(1, Ordering::Relaxed);
    }

    pub fn dec(&self) {
        self.value.fetch_sub(1, Ordering::Relaxed);
    }

    pub fn add(&self, delta: i64) {
        self.value.fetch_add(delta, Ordering::Relaxed);
    }

    pub fn get(&self) -> i64 {
        self.value.load(Ordering::Relaxed)
    }
}

/// A fixed-bucket histogram. Bucket `i` counts observations `<= buckets[i]`;
/// index `buckets.len()` is the `+Inf` bucket.
#[derive(Debug)]
pub struct Histogram {
    name: &'static str,
    help: &'static str,
    buckets: &'static [f64],
    counts: Vec<AtomicU64>,
    /// Sum scaled by 1e6 so an integer atomic can hold fractional seconds.
    sum_micros: AtomicU64,
    count: AtomicU64,
}

impl Histogram {
    pub fn new(name: &'static str, help: &'static str, buckets: &'static [f64]) -> Self {
        let mut counts = Vec::with_capacity(buckets.len() + 1);
        for _ in 0..=buckets.len() {
            counts.push(AtomicU64::new(0));
        }
        Histogram {
            name,
            help,
            buckets,
            counts,
            sum_micros: AtomicU64::new(0),
            count: AtomicU64::new(0),
        }
    }

    pub fn observe(&self, value: f64) {
        if value.is_nan() {
            return;
        }
        let value = value.max(0.0);
        let mut index = self.buckets.len();
        for (i, upper) in self.buckets.iter().enumerate() {
            if value <= *upper {
                index = i;
                break;
            }
        }
        if let Some(slot) = self.counts.get(index) {
            slot.fetch_add(1, Ordering::Relaxed);
        }
        let micros = (value * 1e6).min(u64::MAX as f64) as u64;
        self.sum_micros.fetch_add(micros, Ordering::Relaxed);
        self.count.fetch_add(1, Ordering::Relaxed);
    }

    pub fn observe_seconds(&self, value: f64) {
        self.observe(value);
    }

    pub fn observe_seconds_since(&self, started: Instant) {
        self.observe(started.elapsed().as_secs_f64());
    }

    pub fn observe_millis(&self, millis: f64) {
        self.observe(millis / 1000.0);
    }

    pub fn count(&self) -> u64 {
        self.count.load(Ordering::Relaxed)
    }

    /// Prometheus text for this histogram. `extra_label` is applied to every
    /// series line (used by the labelled family); `emit_header` is false after the
    /// first series of a family, so HELP/TYPE appear exactly once.
    fn render_with(&self, out: &mut String, extra_label: Option<(&str, &str)>, emit_header: bool) {
        if emit_header {
            let _ = writeln!(out, "# HELP {} {}", self.name, self.help);
            let _ = writeln!(out, "# TYPE {} histogram", self.name);
        }
        let tail = match extra_label {
            Some((key, value)) => format!("{key}=\"{}\"", escape_label(value)),
            None => String::new(),
        };
        let mut cumulative: u64 = 0;
        for (i, upper) in self.buckets.iter().enumerate() {
            cumulative = cumulative.saturating_add(self.counts[i].load(Ordering::Relaxed));
            let labels = join_labels(&tail, &format!("le=\"{}\"", format_bound(*upper)));
            let _ = writeln!(out, "{}_bucket{{{labels}}} {cumulative}", self.name);
        }
        cumulative =
            cumulative.saturating_add(self.counts[self.buckets.len()].load(Ordering::Relaxed));
        let labels = join_labels(&tail, "le=\"+Inf\"");
        let _ = writeln!(out, "{}_bucket{{{labels}}} {cumulative}", self.name);
        let sum = self.sum_micros.load(Ordering::Relaxed) as f64 / 1e6;
        let count = self.count.load(Ordering::Relaxed);
        if tail.is_empty() {
            let _ = writeln!(out, "{}_sum {}", self.name, format_float(sum));
            let _ = writeln!(out, "{}_count {count}", self.name);
        } else {
            let _ = writeln!(out, "{}_sum{{{tail}}} {}", self.name, format_float(sum));
            let _ = writeln!(out, "{}_count{{{tail}}} {count}", self.name);
        }
    }

    fn render(&self, out: &mut String) {
        self.render_with(out, None, true);
    }
}

/// A family of counters sharing one name and one label key. The label *values*
/// are drawn from a bounded domain (`level`, `reason`, `lane`, method, status).
#[derive(Debug)]
pub struct LabeledCounter {
    name: &'static str,
    help: &'static str,
    label: &'static str,
    series: Mutex<BTreeMap<String, Arc<Counter>>>,
}

impl LabeledCounter {
    pub fn new(name: &'static str, help: &'static str, label: &'static str) -> Self {
        LabeledCounter {
            name,
            help,
            label,
            series: Mutex::new(BTreeMap::new()),
        }
    }

    /// Fetch (or create) one series. Label values must come from a bounded set.
    pub fn with(&self, value: &str) -> Arc<Counter> {
        let mut series = self.series.lock().unwrap_or_else(|e| e.into_inner());
        bounded_series(&mut series, value, || Arc::new(Counter::default()))
    }

    /// Increment one series (the common case).
    pub fn inc(&self, value: &str) {
        self.with(value).inc();
    }

    pub fn total(&self) -> u64 {
        let series = self.series.lock().unwrap_or_else(|e| e.into_inner());
        series.values().map(|c| c.get()).sum()
    }

    pub fn series_count(&self) -> usize {
        self.series.lock().unwrap_or_else(|e| e.into_inner()).len()
    }

    fn render(&self, out: &mut String) {
        let series = self.series.lock().unwrap_or_else(|e| e.into_inner());
        if series.is_empty() {
            return;
        }
        let _ = writeln!(out, "# HELP {} {}", self.name, self.help);
        let _ = writeln!(out, "# TYPE {} counter", self.name);
        for (label_value, counter) in series.iter() {
            let _ = writeln!(
                out,
                "{}{{{}=\"{}\"}} {}",
                self.name,
                self.label,
                escape_label(label_value),
                counter.get()
            );
        }
    }
}

/// A family of histograms, one per bounded label value.
#[derive(Debug)]
pub struct LabeledHistogram {
    name: &'static str,
    help: &'static str,
    label: &'static str,
    buckets: &'static [f64],
    series: Mutex<BTreeMap<String, Arc<Histogram>>>,
}

impl LabeledHistogram {
    pub fn new(
        name: &'static str,
        help: &'static str,
        label: &'static str,
        buckets: &'static [f64],
    ) -> Self {
        LabeledHistogram {
            name,
            help,
            label,
            buckets,
            series: Mutex::new(BTreeMap::new()),
        }
    }

    pub fn with(&self, value: &str) -> Arc<Histogram> {
        let mut series = self.series.lock().unwrap_or_else(|e| e.into_inner());
        bounded_series(&mut series, value, || {
            Arc::new(Histogram::new(self.name, self.help, self.buckets))
        })
    }

    fn render(&self, out: &mut String) {
        let series = self.series.lock().unwrap_or_else(|e| e.into_inner());
        for (index, (label_value, histogram)) in series.iter().enumerate() {
            histogram.render_with(out, Some((self.label, label_value)), index == 0);
        }
    }
}

/// Map a level string to a bounded label value.
pub fn level_label(level: &str) -> &'static str {
    match level.trim().to_ascii_uppercase().as_str() {
        "TRACE" => "TRACE",
        "DEBUG" => "DEBUG",
        "INFO" => "INFO",
        "SUCCESS" => "SUCCESS",
        "WARN" | "WARNING" => "WARN",
        "ERROR" => "ERROR",
        "CRITICAL" => "CRITICAL",
        _ => "other",
    }
}

/// Map a free-form rejection/status reason to a bounded label value.
pub fn reason_label(reason: &str) -> &'static str {
    match reason {
        "invalid" => "invalid",
        "filtered" => "filtered",
        "overload" => "overload",
        "shutdown" => "shutdown",
        "timeout" => "timeout",
        "storage" => "storage",
        "cancelled" => "cancelled",
        "ok" => "ok",
        _ => "other",
    }
}

/// The gRPC methods this service serves — a closed set, so it is safe as a label.
pub fn method_label(method: &str) -> &'static str {
    match method {
        "Log" => "Log",
        "QueryLogs" => "QueryLogs",
        "LogStats" => "LogStats",
        "PurgeLogs" => "PurgeLogs",
        _ => "other",
    }
}

/// gRPC status codes, mapped to their canonical names.
pub fn status_label(code: i32) -> &'static str {
    match code {
        0 => "OK",
        1 => "CANCELLED",
        2 => "UNKNOWN",
        3 => "INVALID_ARGUMENT",
        4 => "DEADLINE_EXCEEDED",
        5 => "NOT_FOUND",
        6 => "ALREADY_EXISTS",
        7 => "PERMISSION_DENIED",
        8 => "RESOURCE_EXHAUSTED",
        9 => "FAILED_PRECONDITION",
        10 => "ABORTED",
        11 => "OUT_OF_RANGE",
        12 => "UNIMPLEMENTED",
        13 => "INTERNAL",
        14 => "UNAVAILABLE",
        15 => "DATA_LOSS",
        16 => "UNAUTHENTICATED",
        _ => "other",
    }
}

/// `a` is the label prefix owned by the caller (the labelled family's label) and
/// may be empty; `b` is the label that always follows it. They are joined with a
/// comma only when `a` is non-empty, so an unlabelled histogram renders `{le="1"}`
/// and never `{,le="1"}`, and a labelled one renders
/// `{method="Log",le="1"}` with the series' own label first.
fn join_labels(a: &str, b: &str) -> String {
    if a.is_empty() {
        b.to_string()
    } else {
        format!("{a},{b}")
    }
}

fn escape_label(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            c => out.push(c),
        }
    }
    out
}

fn format_bound(bound: f64) -> String {
    if bound.fract() == 0.0 && bound.abs() < 1e15 {
        format!("{}", bound as i64)
    } else {
        format_float(bound)
    }
}

fn format_float(value: f64) -> String {
    if !value.is_finite() {
        return "0".to_string();
    }
    if value.fract() == 0.0 && value.abs() < 1e15 {
        format!("{}", value as i64)
    } else {
        let mut text = format!("{value:.6}");
        while text.ends_with('0') {
            text.pop();
        }
        if text.ends_with('.') {
            text.pop();
        }
        text
    }
}

/// The complete metric registry.
///
/// Everything is `pub` on purpose: the pipeline, storage adapters and transport
/// each own their counters, and a metric that is never read is worse than none.
#[derive(Debug)]
pub struct Metrics {
    pub start: Instant,

    // ---- ingestion ------------------------------------------------------
    /// Events offered by producers (one per `Log` RPC that reached admission).
    pub events_received: Counter,
    /// Events admitted to the pipeline.
    pub events_accepted: LabeledCounter,
    /// Events not admitted, by reason (`invalid`, `filtered`, `overload`,
    /// `shutdown`).
    pub events_rejected: LabeledCounter,
    /// Events dropped by a collection-policy decision (a subset of `rejected`).
    pub events_filtered: LabeledCounter,
    /// Records handed to the storage layer and committed.
    pub events_persisted: Counter,
    /// Records lost after admission, by reason (`overload`, `shutdown`,
    /// `storage`). Not a subset of `rejected`: those never entered the queue.
    pub events_dropped: LabeledCounter,
    /// Write attempts retried after a retryable storage failure.
    pub events_retried: Counter,
    /// Records suppressed by the optional dedupe window.
    pub events_duplicate: Counter,
    /// Records truncated to satisfy a byte limit, by field.
    pub events_truncated: LabeledCounter,
    /// Level strings that were neither canonical nor the `WARNING` alias.
    pub events_unknown_level: Counter,

    // ---- queue and batching --------------------------------------------
    pub queue_depth: LabeledCounter,
    pub queue_capacity: LabeledCounter,
    pub queue_depth_current: Gauge,
    pub queue_capacity_current: Gauge,
    pub queue_rejected_full: LabeledCounter,
    pub queue_wait_seconds: Histogram,
    pub batch_size: Histogram,
    pub batch_flush_seconds: Histogram,
    pub batch_flushes: Counter,
    pub batches_failed: Counter,
    pub pending_writes: Gauge,
    /// Batches currently inside a storage call (0 or 1 per writer thread).
    pub batches_in_flight: Gauge,
    pub in_flight_acks: Gauge,
    pub drain_state: Gauge,
    pub drained_records: Counter,

    // ---- storage --------------------------------------------------------
    pub storage_write_seconds: Histogram,
    pub storage_read_seconds: Histogram,
    pub storage_transaction_failures: Counter,
    pub storage_busy_errors: Counter,
    pub storage_pool_in_use: Gauge,
    pub storage_pool_capacity: Gauge,
    pub storage_pool_wait_seconds: Histogram,
    pub storage_pool_timeouts: Counter,
    pub storage_probe_failures: Counter,
    pub storage_rows: Gauge,

    // ---- retention ------------------------------------------------------
    pub retention_cycles: Counter,
    pub retention_failures: Counter,
    pub retention_purged: Counter,
    pub retention_duration_seconds: Histogram,
    pub retention_protected_rows_skipped: Counter,
    pub retention_budget_exhausted: Counter,
    pub retention_last_success_ts: Gauge,

    // ---- service --------------------------------------------------------
    pub rpc_total: LabeledCounter, // method label; see `rpc_status`
    pub rpc_errors: LabeledCounter,
    pub rpc_duration_seconds: LabeledHistogram,
    pub rpc_in_flight: Gauge,
    pub rpc_timeouts: Counter,
    pub rpc_cancelled: Counter,
    pub redaction_seconds: Histogram,
    pub redaction_hits: Counter,
    pub readiness: Gauge,
    pub liveness: Gauge,
    pub ready: Gauge,
    pub startup_ok: Gauge,
    pub storage_up: Gauge,
    pub pipeline_running: Gauge,

    /// `(method, status) -> count`. Kept separate from `rpc_total` so the bounded
    /// status domain is still exported without a second labelled family per
    /// method.
    rpc_by_status: LabeledCounter,
}

impl Default for Metrics {
    fn default() -> Self {
        Self::new()
    }
}

impl Metrics {
    pub fn new() -> Self {
        let queue_depth = LabeledCounter::new(
            "atf_logger_queue_depth_total",
            "Records enqueued per lane (monotonic lifetime total)",
            "lane",
        );
        queue_depth.with("normal");
        queue_depth.with("priority");
        Metrics {
            start: Instant::now(),
            events_received: Counter::default(),
            events_accepted: LabeledCounter::new(
                "atf_logger_events_accepted_total",
                "Events admitted to the pipeline, by severity",
                "level",
            ),
            events_rejected: LabeledCounter::new(
                "atf_logger_events_rejected_total",
                "Events not admitted, by reason",
                "reason",
            ),
            events_filtered: LabeledCounter::new(
                "atf_logger_events_filtered_total",
                "Events refused by the collection policy, by severity",
                "level",
            ),
            events_persisted: Counter::default(),
            events_dropped: LabeledCounter::new(
                "atf_logger_events_dropped_total",
                "Admitted records that never reached storage, by reason",
                "reason",
            ),
            events_retried: Counter::default(),
            events_duplicate: Counter::default(),
            events_truncated: LabeledCounter::new(
                "atf_logger_events_truncated_total",
                "Records truncated to fit a byte budget, by field",
                "field",
            ),
            events_unknown_level: Counter::default(),
            queue_depth,
            queue_capacity: LabeledCounter::new(
                "atf_logger_queue_capacity",
                "Bounded capacity per lane",
                "lane",
            ),
            queue_depth_current: Gauge::default(),
            queue_capacity_current: Gauge::default(),
            queue_rejected_full: LabeledCounter::new(
                "atf_logger_queue_full_total",
                "Admissions refused because the lane was full, by lane",
                "lane",
            ),
            queue_wait_seconds: Histogram::new(
                "atf_logger_queue_wait_seconds",
                "Time a producer-side record waited before it was admitted",
                LATENCY_BUCKETS,
            ),
            batch_size: Histogram::new(
                "atf_logger_batch_size",
                "Records per storage transaction",
                BATCH_BUCKETS,
            ),
            batch_flush_seconds: Histogram::new(
                "atf_logger_batch_flush_seconds",
                "Wall time to commit one batch",
                LATENCY_BUCKETS,
            ),
            batch_flushes: Counter::default(),
            batches_failed: Counter::default(),
            pending_writes: Gauge::default(),
            batches_in_flight: Gauge::default(),
            in_flight_acks: Gauge::default(),
            drain_state: Gauge::default(),
            drained_records: Counter::default(),
            storage_write_seconds: Histogram::new(
                "atf_logger_storage_write_seconds",
                "Wall time of one storage write transaction",
                LATENCY_BUCKETS,
            ),
            storage_read_seconds: Histogram::new(
                "atf_logger_storage_read_seconds",
                "Wall time of one storage read statement",
                LATENCY_BUCKETS,
            ),
            storage_transaction_failures: Counter::default(),
            storage_busy_errors: Counter::default(),
            storage_pool_in_use: Gauge::default(),
            storage_pool_capacity: Gauge::default(),
            storage_pool_wait_seconds: Histogram::new(
                "atf_logger_storage_pool_wait_seconds",
                "Time spent waiting for a pooled connection",
                LATENCY_BUCKETS,
            ),
            storage_pool_timeouts: Counter::default(),
            storage_probe_failures: Counter::default(),
            storage_rows: Gauge::default(),
            retention_cycles: Counter::default(),
            retention_failures: Counter::default(),
            retention_purged: Counter::default(),
            retention_duration_seconds: Histogram::new(
                "atf_logger_retention_duration_seconds",
                "Wall time of one retention cycle",
                LATENCY_BUCKETS,
            ),
            retention_protected_rows_skipped: Counter::default(),
            retention_budget_exhausted: Counter::default(),
            retention_last_success_ts: Gauge::default(),
            rpc_total: LabeledCounter::new(
                "atf_logger_rpc_total",
                "gRPC calls served, by method",
                "method",
            ),
            rpc_errors: LabeledCounter::new(
                "atf_logger_rpc_failed_total",
                "gRPC calls that returned a non-OK status, by status code",
                "status",
            ),
            rpc_duration_seconds: LabeledHistogram::new(
                "atf_logger_rpc_duration_seconds",
                "gRPC handler latency",
                "method",
                LATENCY_BUCKETS,
            ),
            rpc_in_flight: Gauge::default(),
            rpc_timeouts: Counter::default(),
            rpc_cancelled: Counter::default(),
            redaction_seconds: Histogram::new(
                "atf_logger_redaction_seconds",
                "Time spent scrubbing one record before persistence",
                LATENCY_BUCKETS,
            ),
            redaction_hits: Counter::default(),
            readiness: Gauge::default(),
            liveness: Gauge::default(),
            ready: Gauge::default(),
            startup_ok: Gauge::default(),
            storage_up: Gauge::default(),
            pipeline_running: Gauge::default(),
            rpc_by_status: LabeledCounter::new(
                "atf_logger_rpc_status_total",
                "gRPC calls by status code",
                "status",
            ),
        }
    }

    /// Record one RPC outcome. `status_code` is a tonic/gRPC code.
    pub fn record_rpc(&self, method: &str, status_code: i32, elapsed: Duration) {
        self.rpc_total.inc(method_label(method));
        self.rpc_by_status.inc(status_label(status_code));
        if status_code != 0 {
            self.rpc_errors.inc(status_label(status_code));
        }
        self.rpc_duration_seconds
            .with(method_label(method))
            .observe_seconds(elapsed.as_secs_f64());
    }

    pub fn uptime_seconds(&self) -> f64 {
        self.start.elapsed().as_secs_f64()
    }

    /// Render the full Prometheus text exposition.
    pub fn render(
        &self,
        health: &HealthState,
        config_version: &str,
        backend: &str,
        ack_mode: &str,
    ) -> String {
        let mut out = String::with_capacity(16 * 1024);

        // --- ingestion ---------------------------------------------------
        let _ = writeln!(
            out,
            "# HELP atf_logger_events_received_total Events offered by producers"
        );
        let _ = writeln!(out, "# TYPE atf_logger_events_received_total counter");
        let _ = writeln!(
            out,
            "atf_logger_events_received_total {}",
            self.events_received.get()
        );
        let _ = writeln!(
            out,
            "# HELP atf_logger_events_persisted_total Records committed to storage"
        );
        let _ = writeln!(out, "# TYPE atf_logger_events_persisted_total counter");
        let _ = writeln!(
            out,
            "atf_logger_events_persisted_total {}",
            self.events_persisted.get()
        );
        for family in [
            &self.events_accepted,
            &self.events_rejected,
            &self.events_filtered,
            &self.events_dropped,
            &self.events_truncated,
        ] {
            family.render(&mut out);
        }
        let _ = writeln!(out, "# HELP atf_logger_events_retried_total Write attempts retried after a retryable failure");
        let _ = writeln!(out, "# TYPE atf_logger_events_retried_total counter");
        let _ = writeln!(
            out,
            "atf_logger_events_retried_total {}",
            self.events_retried.get()
        );
        let _ = writeln!(
            out,
            "# HELP atf_logger_events_duplicate_total Records suppressed by the dedupe window"
        );
        let _ = writeln!(out, "# TYPE atf_logger_events_duplicate_total counter");
        let _ = writeln!(
            out,
            "atf_logger_events_duplicate_total {}",
            self.events_duplicate.get()
        );
        let _ = writeln!(
            out,
            "# HELP atf_logger_events_unknown_level_total Level strings outside the canonical set"
        );
        let _ = writeln!(out, "# TYPE atf_logger_events_unknown_level_total counter");
        let _ = writeln!(
            out,
            "atf_logger_events_unknown_level_total {}",
            self.events_unknown_level.get()
        );

        // --- queue and batching ------------------------------------------
        self.queue_depth.render(&mut out);
        self.queue_capacity.render(&mut out);
        self.queue_rejected_full.render(&mut out);
        for (name, help, gauge) in [
            (
                "atf_logger_queue_depth",
                "Records waiting in the bounded queue",
                &self.queue_depth_current,
            ),
            (
                "atf_logger_queue_capacity",
                "Configured total queue slots",
                &self.queue_capacity_current,
            ),
            (
                "atf_logger_pending_writes",
                "Records admitted but not yet committed",
                &self.pending_writes,
            ),
            (
                "atf_logger_batches_in_flight",
                "Batches currently inside a storage call",
                &self.batches_in_flight,
            ),
            (
                "atf_logger_in_flight_acks",
                "Producer RPCs waiting for an acknowledgement",
                &self.in_flight_acks,
            ),
        ] {
            let _ = writeln!(out, "# HELP {name} {help}");
            let _ = writeln!(out, "# TYPE {name} gauge");
            let _ = writeln!(out, "{name} {}", gauge.get());
        }
        let _ = writeln!(
            out,
            "# HELP atf_logger_queue_utilization Queue depth as a fraction of capacity"
        );
        let _ = writeln!(out, "# TYPE atf_logger_queue_utilization gauge");
        let capacity = self.queue_capacity_current.get().max(0) as f64;
        let depth = self.queue_depth_current.get().max(0) as f64;
        let utilization = if capacity > 0.0 {
            depth / capacity
        } else {
            0.0
        };
        let _ = writeln!(out, "atf_logger_queue_utilization {:.6}", utilization);
        let _ = writeln!(
            out,
            "# HELP atf_logger_drain_state Drain state (0=running, 1=draining, 2=drained)"
        );
        let _ = writeln!(out, "# TYPE atf_logger_drain_state gauge");
        let _ = writeln!(out, "atf_logger_drain_state {}", self.drain_state.get());
        let _ = writeln!(
            out,
            "# HELP atf_logger_drained_records_total Records flushed by the graceful drain"
        );
        let _ = writeln!(out, "# TYPE atf_logger_drained_records_total counter");
        let _ = writeln!(
            out,
            "atf_logger_drained_records_total {}",
            self.drained_records.get()
        );
        for histogram in [
            &self.queue_wait_seconds,
            &self.batch_size,
            &self.batch_flush_seconds,
        ] {
            histogram.render(&mut out);
        }
        for (name, help, counter) in [
            (
                "atf_logger_batch_flushes_total",
                "Batches committed",
                &self.batch_flushes,
            ),
            (
                "atf_logger_batches_failed_total",
                "Batches that failed after every retry",
                &self.batches_failed,
            ),
        ] {
            let _ = writeln!(out, "# HELP {name} {help}");
            let _ = writeln!(out, "# TYPE {name} counter");
            let _ = writeln!(out, "{name} {}", counter.get());
        }

        // --- storage ------------------------------------------------------
        for histogram in [
            &self.storage_write_seconds,
            &self.storage_read_seconds,
            &self.storage_pool_wait_seconds,
        ] {
            histogram.render(&mut out);
        }
        for (name, help, counter) in [
            (
                "atf_logger_storage_transaction_failures_total",
                "Storage transactions that failed",
                &self.storage_transaction_failures,
            ),
            (
                "atf_logger_storage_busy_errors_total",
                "Storage busy/lock errors",
                &self.storage_busy_errors,
            ),
            (
                "atf_logger_storage_pool_timeouts_total",
                "Pool acquisitions that timed out",
                &self.storage_pool_timeouts,
            ),
            (
                "atf_logger_storage_probe_failures_total",
                "Readiness probes that failed",
                &self.storage_probe_failures,
            ),
        ] {
            let _ = writeln!(out, "# HELP {name} {help}");
            let _ = writeln!(out, "# TYPE {name} counter");
            let _ = writeln!(out, "{name} {}", counter.get());
        }
        for (name, help, gauge) in [
            (
                "atf_logger_storage_pool_in_use",
                "Connections currently leased from the pool",
                &self.storage_pool_in_use,
            ),
            (
                "atf_logger_storage_pool_capacity",
                "Maximum pooled connections",
                &self.storage_pool_capacity,
            ),
            (
                "atf_logger_storage_rows",
                "Rows currently in the archive (last observed)",
                &self.storage_rows,
            ),
        ] {
            let _ = writeln!(out, "# HELP {name} {help}");
            let _ = writeln!(out, "# TYPE {name} gauge");
            let _ = writeln!(out, "{name} {}", gauge.get());
        }

        // --- retention ----------------------------------------------------
        self.retention_duration_seconds.render(&mut out);
        for (name, help, counter) in [
            (
                "atf_logger_retention_cycles_total",
                "Retention cycles completed",
                &self.retention_cycles,
            ),
            (
                "atf_logger_retention_failures_total",
                "Retention cycles that failed",
                &self.retention_failures,
            ),
            (
                "atf_logger_retention_purged_total",
                "Rows removed by retention",
                &self.retention_purged,
            ),
            (
                "atf_logger_retention_protected_rows_total",
                "Purge candidates spared because their category is protected",
                &self.retention_protected_rows_skipped,
            ),
            (
                "atf_logger_retention_budget_exhausted_total",
                "Cycles that stopped early because the work budget ran out",
                &self.retention_budget_exhausted,
            ),
        ] {
            let _ = writeln!(out, "# HELP {name} {help}");
            let _ = writeln!(out, "# TYPE {name} counter");
            let _ = writeln!(out, "{name} {}", counter.get());
        }
        let _ = writeln!(out, "# HELP atf_logger_retention_last_success_timestamp_seconds Unix time of the last successful retention cycle");
        let _ = writeln!(
            out,
            "# TYPE atf_logger_retention_last_success_timestamp_seconds gauge"
        );
        let _ = writeln!(
            out,
            "atf_logger_retention_last_success_timestamp_seconds {}",
            self.retention_last_success_ts.get()
        );

        // --- service ------------------------------------------------------
        self.rpc_total.render(&mut out);
        self.rpc_errors.render(&mut out);
        // RPC by method (`rpc_total`) and by bounded status (`rpc_status_total`) are
        // separate families; the status one is what makes an INTERNAL/UNAVAILABLE
        // ratio visible without a per-method label explosion.
        self.rpc_by_status.render(&mut out);
        self.rpc_duration_seconds.render(&mut out);
        self.redaction_seconds.render(&mut out);
        for (name, help, value) in [
            (
                "atf_logger_rpc_in_flight",
                "gRPC handlers currently running",
                self.rpc_in_flight.get(),
            ),
            (
                "atf_logger_liveness",
                "1 when the process is alive",
                self.liveness.get(),
            ),
            (
                "atf_logger_readiness",
                "1 when the service can fulfil its role",
                self.readiness.get(),
            ),
            (
                "atf_logger_ready",
                "1 when ready, 0 when not",
                self.ready.get(),
            ),
            (
                "atf_logger_storage_up",
                "1 when the configured backend is reachable",
                self.storage_up.get(),
            ),
            (
                "atf_logger_pipeline_running",
                "1 when the writer pipeline is running",
                self.pipeline_running.get(),
            ),
            (
                "atf_logger_startup_ok",
                "1 when startup completed",
                self.startup_ok.get(),
            ),
        ] {
            let _ = writeln!(out, "# HELP {name} {help}");
            let _ = writeln!(out, "# TYPE {name} gauge");
            let _ = writeln!(out, "{name} {value}");
        }
        let _ = writeln!(
            out,
            "# HELP atf_logger_rpc_timeouts_total Producer RPCs that exceeded the ack deadline"
        );
        let _ = writeln!(out, "# TYPE atf_logger_rpc_timeouts_total counter");
        let _ = writeln!(
            out,
            "atf_logger_rpc_timeouts_total {}",
            self.rpc_timeouts.get()
        );
        let _ = writeln!(
            out,
            "# HELP atf_logger_rpc_cancelled_total Producer RPCs cancelled by the client"
        );
        let _ = writeln!(out, "# TYPE atf_logger_rpc_cancelled_total counter");
        let _ = writeln!(
            out,
            "atf_logger_rpc_cancelled_total {}",
            self.rpc_cancelled.get()
        );
        let _ = writeln!(
            out,
            "# HELP atf_logger_redaction_hits_total Records in which redaction changed something"
        );
        let _ = writeln!(out, "# TYPE atf_logger_redaction_hits_total counter");
        let _ = writeln!(
            out,
            "atf_logger_redaction_hits_total {}",
            self.redaction_hits.get()
        );
        let _ = writeln!(out, "# HELP atf_logger_uptime_seconds Process uptime");
        let _ = writeln!(out, "# TYPE atf_logger_uptime_seconds gauge");
        let _ = writeln!(
            out,
            "atf_logger_uptime_seconds {:.3}",
            self.uptime_seconds()
        );

        // --- info / health -------------------------------------------------
        // Bounded labels only: version, backend, ack mode, config fingerprint and
        // a coarse health state. Never a tenant or message identifier.
        let _ = writeln!(
            out,
            "# HELP atf_logger_info Build and configuration identity"
        );
        let _ = writeln!(out, "# TYPE atf_logger_info gauge");
        let _ = writeln!(
            out,
            "atf_logger_info{{version=\"{}\",backend=\"{}\",ack_mode=\"{}\"}} 1",
            escape_label(crate::VERSION),
            escape_label(backend),
            escape_label(ack_mode)
        );
        let _ = writeln!(
            out,
            "# HELP atf_logger_config_info Effective configuration fingerprint (bounded label set)"
        );
        let _ = writeln!(out, "# TYPE atf_logger_config_info gauge");
        let _ = writeln!(
            out,
            "atf_logger_config_info{{config_version=\"{}\",backend=\"{}\",ack_mode=\"{}\"}} 1",
            escape_label(config_version),
            escape_label(backend),
            escape_label(ack_mode)
        );
        let _ = writeln!(
            out,
            "# HELP atf_logger_health Health state as a bounded label plus reason code"
        );
        let _ = writeln!(out, "# TYPE atf_logger_health gauge");
        let _ = writeln!(
            out,
            "atf_logger_health{{state=\"{}\",reason=\"{}\"}} {}",
            health.state_label(),
            health.reason_code(),
            if health.is_ready() { 1 } else { 0 }
        );
        out
    }

    /// JSON snapshot for the `/healthz` and `/readyz` bodies.
    pub fn health_json(health: &HealthState) -> String {
        format!(
            "{{\"status\":\"{}\",\"ready\":{},\"alive\":{},\"reason\":\"{}\",\"detail\":\"{}\"}}",
            health.state_label(),
            health.is_ready(),
            health.is_alive(),
            health.reason_code(),
            escape_json(&health.detail())
        )
    }
}

fn escape_json(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out
}

/// A minimal HTTP/1.1 listener for `/metrics`, `/healthz` and `/readyz`.
///
/// Deliberately hand-rolled: it must not depend on the gRPC runtime, the storage
/// layer or the logger's own diagnostics, so a broken database still leaves this
/// endpoint answering. Requests are read line-wise with a small bounded buffer and
/// the connection is closed after one response (Prometheus scrapes with a fresh
/// connection; there is no benefit to keep-alive here).
pub async fn serve(
    listener: tokio::net::TcpListener,
    metrics: Arc<Metrics>,
    health: Arc<HealthState>,
    config_version: String,
    backend: String,
    ack_mode: String,
) {
    loop {
        let (mut socket, _peer) = match listener.accept().await {
            Ok(pair) => pair,
            Err(_) => continue,
        };
        let metrics = Arc::clone(&metrics);
        let health = Arc::clone(&health);
        let config_version = config_version.clone();
        let backend = backend.clone();
        let ack_mode = ack_mode.clone();
        tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let mut buffer = [0u8; 1024];
            let read = match tokio::time::timeout(Duration::from_secs(5), socket.read(&mut buffer))
                .await
            {
                Ok(Ok(n)) => n,
                _ => return, // no request (or a slow one): drop the connection
            };
            let request = String::from_utf8_lossy(&buffer[..read]);
            let path = request
                .lines()
                .next()
                .and_then(|line| line.split_whitespace().nth(1))
                .unwrap_or("/");
            let (status, content_type, body) = match path {
                "/metrics" => (
                    "200 OK",
                    "text/plain; version=0.0.4; charset=utf-8",
                    metrics.render(&health, &config_version, &backend, &ack_mode),
                ),
                "/healthz" => ("200 OK", "application/json", Metrics::health_json(&health)),
                "/readyz" => {
                    let code = if health.is_ready() {
                        "200 OK"
                    } else {
                        "503 Service Unavailable"
                    };
                    (code, "application/json", Metrics::health_json(&health))
                }
                _ => ("404 Not Found", "text/plain", "not found\n".to_string()),
            };
            let response = format!(
                "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = socket.write_all(response.as_bytes()).await;
            let _ = socket.flush().await;
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::health::{HealthState, REASON_STORAGE_UNAVAILABLE};

    #[test]
    fn counters_and_gauges_move() {
        let counter = Counter::default();
        counter.inc();
        counter.add(4);
        assert_eq!(counter.get(), 5);
        let gauge = Gauge::default();
        gauge.set(3);
        gauge.dec();
        gauge.add(2);
        assert_eq!(gauge.get(), 4);
    }

    #[test]
    fn histogram_buckets_are_cumulative() {
        let histogram = Histogram::new("h", "help", &[1.0, 2.0]);
        histogram.observe(0.5);
        histogram.observe(1.5);
        histogram.observe(9.0);
        assert_eq!(histogram.count(), 3);
        let mut out = String::new();
        histogram.render(&mut out);
        assert!(out.contains("h_bucket{le=\"1\"} 1"), "{out}");
        assert!(out.contains("h_bucket{le=\"2\"} 2"), "{out}");
        assert!(out.contains("h_bucket{le=\"+Inf\"} 3"), "{out}");
        assert!(out.contains("h_count 3"), "{out}");
        // sum = 0.5 + 1.5 + 9 = 11
        assert!(out.contains("h_sum 11"), "{out}");
    }

    #[test]
    fn labeled_family_is_bounded_and_escapes() {
        let family = LabeledCounter::new("f", "help", "level");
        family.inc("ERROR");
        family.inc("ERROR");
        family.inc("WARN");
        assert_eq!(family.with("ERROR").get(), 2);
        assert_eq!(family.total(), 3);
        for i in 0..(MAX_SERIES * 2) {
            family.inc(&format!("unknown-{i}"));
        }
        assert!(family.series_count() <= MAX_SERIES);
        let escaped = LabeledCounter::new("g", "help", "v");
        escaped.inc("a\"b\\c");
        let mut out = String::new();
        escaped.render(&mut out);
        assert!(out.contains("v=\"a\\\"b\\\\c\""), "{out}");
    }

    #[test]
    fn labels_are_bounded_for_hostile_input() {
        assert_eq!(level_label("error"), "ERROR");
        assert_eq!(level_label("warning"), "WARN");
        assert_eq!(level_label("chat-12345"), "other");
        assert_eq!(reason_label("something-new"), "other");
        assert_eq!(method_label("DeleteEverything"), "other");
        assert_eq!(status_label(13), "INTERNAL");
        assert_eq!(status_label(99), "other");
    }

    #[test]
    fn render_includes_every_required_family_and_no_high_cardinality_labels() {
        let metrics = Metrics::new();
        metrics.events_received.inc();
        metrics.events_accepted.inc("ERROR");
        metrics.events_rejected.inc("filtered");
        metrics.events_filtered.inc("INFO");
        metrics.events_persisted.inc();
        metrics.events_dropped.inc("overload");
        metrics.events_retried.inc();
        metrics.events_duplicate.inc();
        metrics.record_rpc("Log", 0, Duration::from_millis(1));
        metrics.record_rpc("QueryLogs", 13, Duration::from_millis(2));
        let health = HealthState::new();
        health.mark_ready("storage probe ok");
        let text = metrics.render(&health, "deadbeef", "sqlite", "durable");
        for expected in [
            "atf_logger_events_received_total 1",
            "atf_logger_events_accepted_total{level=\"ERROR\"} 1",
            "atf_logger_events_rejected_total{reason=\"filtered\"} 1",
            "atf_logger_events_filtered_total{level=\"INFO\"} 1",
            "atf_logger_events_persisted_total 1",
            "atf_logger_events_dropped_total{reason=\"overload\"} 1",
            "atf_logger_events_retried_total 1",
            "atf_logger_events_duplicate_total 1",
            "atf_logger_batch_flush_seconds_bucket{le=\"+Inf\"}",
            "atf_logger_storage_write_seconds_bucket",
            "atf_logger_retention_duration_seconds_count",
            "atf_logger_rpc_total{method=\"Log\"} 1",
            "atf_logger_rpc_status_total{status=\"INTERNAL\"} 1",
            "atf_logger_rpc_duration_seconds_bucket{method=\"Log\",le=\"+Inf\"}",
            "atf_logger_rpc_duration_seconds_count{method=\"Log\"} 1",
            "atf_logger_redaction_seconds_count",
            "atf_logger_config_info{config_version=\"deadbeef\",backend=\"sqlite\",ack_mode=\"durable\"} 1",
            "atf_logger_info{version=",
        ] {
            assert!(text.contains(expected), "missing {expected} in\n{text}");
        }
        // The health state is a bounded label pair, not free text.
        assert!(text.contains("state=\"ready\""));
        // No label may be empty-braced and no series may carry a tenant id.
        assert!(!text.contains("{,}"));
    }

    #[test]
    fn health_json_is_valid_json_ish_and_escapes() {
        let health = HealthState::new();
        health.mark_not_ready(REASON_STORAGE_UNAVAILABLE, "quote \" and newline\n");
        let body = Metrics::health_json(&health);
        assert!(body.contains("\"ready\":false"));
        assert!(body.contains("\\\""));
        assert!(!body.contains('\n'));
        assert!(body.contains(REASON_STORAGE_UNAVAILABLE));
    }

    #[tokio::test]
    async fn http_listener_answers_metrics_health_and_404() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let metrics = Arc::new(Metrics::new());
        let health = Arc::new(HealthState::new());
        health.mark_ready("probe ok");
        let server = tokio::spawn(serve(
            listener,
            Arc::clone(&metrics),
            Arc::clone(&health),
            "v".to_string(),
            "sqlite".to_string(),
            "durable".to_string(),
        ));

        let fetch = |path: &'static str| async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let mut stream = tokio::net::TcpStream::connect(addr).await.unwrap();
            stream
                .write_all(format!("GET {path} HTTP/1.1\r\nHost: x\r\n\r\n").as_bytes())
                .await
                .unwrap();
            let mut body = String::new();
            stream.read_to_string(&mut body).await.unwrap();
            body
        };

        let metrics_body = fetch("/metrics").await;
        assert!(
            metrics_body.starts_with("HTTP/1.1 200 OK"),
            "{metrics_body}"
        );
        assert!(metrics_body.contains("atf_logger_events_received_total"));
        let ready_body = fetch("/readyz").await;
        assert!(ready_body.contains("\"ready\":true"), "{ready_body}");
        let missing = fetch("/nope").await;
        assert!(missing.starts_with("HTTP/1.1 404"), "{missing}");
        server.abort();
    }

    #[test]
    fn cardinality_guard_stays_at_the_cap_and_never_loses_a_count() {
        // The cap has to include the overflow series itself: the family may hold at
        // most MAX_SERIES series, and every folded increment must still be visible.
        let family = LabeledCounter::new("f", "help", "level");
        family.inc("ERROR");
        family.inc("ERROR");
        family.inc("WARN");
        let hostile = MAX_SERIES * 4;
        for i in 0..hostile {
            family.inc(&format!("hostile-{i}"));
        }
        assert!(
            family.series_count() <= MAX_SERIES,
            "{} series for a cap of {MAX_SERIES}",
            family.series_count()
        );
        assert_eq!(family.with("ERROR").get(), 2);
        assert_eq!(family.total(), 3 + hostile as u64, "folding lost counts");
        let mut out = String::new();
        family.render(&mut out);
        let rendered = out
            .lines()
            .filter(|line| line.starts_with("f{level="))
            .count();
        assert!(rendered <= MAX_SERIES, "{rendered} series rendered:\n{out}");
        assert!(out.contains("level=\"other\""), "{out}");

        // The same bound applies to a labelled histogram family.
        let histograms = LabeledHistogram::new("h", "help", "method", LATENCY_BUCKETS);
        for i in 0..hostile {
            histograms
                .with(&format!("method-{i}"))
                .observe_seconds(0.001);
        }
        let mut out = String::new();
        histograms.render(&mut out);
        let rendered = out.matches("h_count{method=").count();
        assert!(rendered <= MAX_SERIES, "{rendered} series rendered:\n{out}");
        let named = (MAX_SERIES - 1) as u64;
        assert!(
            out.contains(&format!(
                "h_count{{method=\"{OVERFLOW_LABEL}\"}} {}",
                hostile as u64 - named
            )),
            "the overflow series must carry every folded observation:\n{out}"
        );
    }

    #[test]
    fn rpc_status_family_is_exported_and_bounded() {
        let metrics = Metrics::new();
        metrics.record_rpc("Log", 0, Duration::from_millis(1));
        metrics.record_rpc("QueryLogs", 13, Duration::from_millis(2));
        metrics.record_rpc("PurgeLogs", 7, Duration::from_millis(3));
        for code in 0..(MAX_SERIES * 2) {
            metrics.record_rpc("Log", 100 + code as i32, Duration::from_millis(1));
        }
        let health = HealthState::new();
        health.mark_ready("probe ok");
        let text = metrics.render(&health, "deadbeef", "sqlite", "durable");
        for expected in [
            "atf_logger_rpc_status_total{status=\"OK\"} 1",
            "atf_logger_rpc_status_total{status=\"INTERNAL\"} 1",
            "atf_logger_rpc_status_total{status=\"PERMISSION_DENIED\"} 1",
            &format!(
                "atf_logger_rpc_status_total{{status=\"{OVERFLOW_LABEL}\"}} {}",
                MAX_SERIES * 2
            ),
        ] {
            assert!(text.contains(expected), "missing {expected} in\n{text}");
        }
        let status_series = text
            .lines()
            .filter(|line| line.starts_with("atf_logger_rpc_status_total{"))
            .count();
        assert!(
            status_series <= MAX_SERIES,
            "{status_series} status series for a cap of {MAX_SERIES}"
        );
        assert!(
            text.contains("# TYPE atf_logger_rpc_status_total counter"),
            "{text}"
        );
        assert!(
            text.contains("# HELP atf_logger_rpc_status_total gRPC calls by status code"),
            "{text}"
        );
    }

    #[test]
    fn labelled_histogram_puts_the_series_label_before_le() {
        let histograms = LabeledHistogram::new("h", "help", "method", &[1.0]);
        histograms.with("Log").observe_seconds(0.5);
        let mut out = String::new();
        histograms.render(&mut out);
        assert!(out.contains("h_bucket{method=\"Log\",le=\"1\"} 1"), "{out}");
        assert!(
            out.contains("h_bucket{method=\"Log\",le=\"+Inf\"} 1"),
            "{out}"
        );
        assert!(out.contains("h_count{method=\"Log\"} 1"), "{out}");
        // An unlabelled histogram must not grow an empty label.
        let plain = Histogram::new("p", "help", &[1.0]);
        plain.observe_seconds(0.5);
        let mut out = String::new();
        plain.render(&mut out);
        assert!(out.contains("p_bucket{le=\"1\"} 1"), "{out}");
        assert!(!out.contains("{,"), "{out}");
    }
}
