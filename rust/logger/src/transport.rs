//! The gRPC `LogControlService` implementation.
//!
//! This is the whole public surface of the Rust logger: four RPCs, exactly as
//! `proto/autoforward.proto` defines them, exactly as `logger/log_client.py` and
//! the Go API call them. There is no REST API here and there never will be —
//! business/domain HTTP lives in Go, and the only listener Rust owns besides this
//! one is the metrics/health endpoint.
//!
//! ## Ack honesty
//!
//! `StatusResponse.success == true` means what the configured [`AckMode`] says it
//! means:
//!
//! * `durable` — the record's batch **committed**. The RPC waits for the writer.
//! * `queued` — the record is in the bounded queue. That is a weaker promise, so
//!   readiness is reported as `degraded` with reason `ack_mode_queued`
//!   (see [`crate::health`]) rather than advertised as durable.
//!
//! Every failure path answers `success=false` with a stable `error_code` and a
//! message: `INVALID`, `FILTERED`, `OVERLOAD`, `SHUTTING_DOWN`, `DUPLICATE`,
//! `TIMEOUT`, `STORAGE_*`. Nothing is accepted and then silently dropped.
//!
//! ## Redaction before persistence
//!
//! [`crate::ingest::prepare`] redacts *and* bounds every field before the record is
//! admitted to the pipeline, so neither SQLite nor PostgreSQL (nor any future
//! exporter reading them) can ever hold a bot token, session string, password or
//! authorization header.

use crate::config::{Config, TsUnit};
use crate::health::{HealthState, REASON_ACK_MODE_QUEUED};
use crate::ingest::{self, CollectPolicy, Limits, RawEntry, Reject};
use crate::metrics::{self, Metrics};
use crate::pb;
use crate::pipeline::{Ack, AdmitError, Pipeline};
use crate::query::{
    self, Exporter, GroupBy, Page, PurgeFilter, QueryFilter, StatsScope, MAX_PAGE,
};
use crate::storage::{PurgeReport, Record, Storage, StorageError};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

/// Rows a single `PurgeLogs` call may delete. A purge with no time bound is a
/// legitimate operator action (the Python engine supports it), but it is capped so
/// one call cannot lock the archive for an hour; the response reports whether the
/// cap stopped it.
pub const MAX_PURGE_ROWS: u64 = 1_000_000;

/// Stable error code for a record that never reached storage.
pub const CODE_DROPPED: &str = "STORAGE_DROPPED";

/// The gRPC service.
pub struct LogControl {
    pipeline: Arc<Pipeline>,
    storage: Arc<dyn Storage>,
    metrics: Arc<Metrics>,
    health: Arc<HealthState>,
    policy: CollectPolicy,
    limits: Limits,
    ts_unit: TsUnit,
    protected_categories: Vec<String>,
    redact_identifiers: bool,
    ack_timeout: Duration,
    admission_wait: Duration,
    purge_timeout: Duration,
    config_version: String,
    backend: String,
    ack_mode: &'static str,
}

impl LogControl {
    pub fn new(
        config: &Config,
        policy: CollectPolicy,
        pipeline: Arc<Pipeline>,
        storage: Arc<dyn Storage>,
        metrics: Arc<Metrics>,
        health: Arc<HealthState>,
    ) -> Self {
        // A durable ack is bounded by `ack_timeout_ms`; the time a producer may
        // spend waiting for a queue slot is capped well below it so a saturated
        // queue answers "overload" instead of burning the whole ack budget.
        let admission_wait = Duration::from_millis(config.ack_timeout_ms.min(1_000).max(1));
        LogControl {
            pipeline,
            storage,
            metrics,
            health,
            policy,
            limits: Limits::from_config(config),
            ts_unit: config.retention_ts_unit,
            protected_categories: config.protected_categories.clone(),
            redact_identifiers: config.redact_identifiers,
            ack_timeout: Duration::from_millis(config.ack_timeout_ms.max(1)),
            admission_wait,
            purge_timeout: Duration::from_millis(config.purge_timeout_ms.max(1)),
            config_version: config.version_hash(),
            backend: String::new(),
            ack_mode: config.ack_mode.as_str(),
        }
    }

    /// Record the storage backend's identity for diagnostics/metrics.
    pub fn with_backend(mut self, backend: impl Into<String>) -> Self {
        self.backend = backend.into();
        self
    }

    /// Wrap in the generated tonic server type.
    pub fn into_server(self) -> pb::log_control_service_server::LogControlServiceServer<Self> {
        pb::log_control_service_server::LogControlServiceServer::new(self)
    }

    pub fn config_version(&self) -> &str {
        &self.config_version
    }

    pub fn backend(&self) -> &str {
        &self.backend
    }

    pub fn ack_mode(&self) -> &'static str {
        self.ack_mode
    }

    // ---- internal helpers -------------------------------------------------

    fn status(&self, success: bool, code: &str, message: impl Into<String>) -> pb::StatusResponse {
        pb::StatusResponse {
            success,
            message: message.into(),
            error_code: if success { String::new() } else { code.to_string() },
        }
    }

    /// Count a refusal in the same place, whatever the reason, so the
    /// received/accepted/rejected accounting always adds up.
    fn reject(&self, code: &str, level: Option<&str>, filtered: bool) -> pb::StatusResponse {
        let reason = match code {
            "FILTERED" => "filtered",
            "INVALID" => "invalid",
            "OVERLOAD" => "overload",
            "SHUTTING_DOWN" => "shutdown",
            "DUPLICATE" => "duplicate",
            "TIMEOUT" => "timeout",
            _ => "storage",
        };
        self.metrics.events_rejected.inc(metrics::reason_label(reason));
        if filtered {
            self.metrics
                .events_filtered
                .inc(metrics::level_label(level.unwrap_or("other")));
        }
        pb::StatusResponse {
            success: false,
            message: String::new(),
            error_code: code.to_string(),
        }
    }

    /// Admit one prepared record, waiting a bounded time for a queue slot.
    ///
    /// Backpressure is a *bounded wait then a refusal*, never an unbounded buffer:
    /// a burst becomes latency while the queue has room, and an outage becomes a
    /// counted `OVERLOAD` answer once it does not.
    async fn admit_with_backpressure(
        &self,
        prepared: crate::ingest::Prepared,
    ) -> Result<Ack, AdmitError> {
        let deadline = Instant::now() + self.admission_wait;
        let mut attempt = prepared;
        loop {
            // `admit` consumes the record (it is moved into the queue, or dropped by
            // a failed push), so a retry needs a copy. The copy is cheap and it is
            // only ever made for a record that was *refused*, never one that landed.
            let retry = attempt.clone();
            match self.pipeline.admit(attempt) {
                Ok(ack) => return Ok(ack),
                Err(err @ AdmitError::Full { .. }) => {
                    if Instant::now() >= deadline {
                        return Err(err);
                    }
                    tokio::time::sleep(Duration::from_millis(1)).await;
                    attempt = retry;
                }
                Err(err) => return Err(err),
            }
        }
    }

    // ---- query helpers (not on the wire; used by tests and the admin surface) ----

    /// Most recent records for a scope, newest first, bounded.
    pub fn recent(&self, filter: &QueryFilter, limit: usize) -> Result<Vec<Record>, StorageError> {
        let page = Page {
            limit: limit.clamp(1, MAX_PAGE),
            offset: 0,
            ascending: false,
            keyset: None,
        };
        let (rows, _) = self.storage.query(filter, &page)?;
        Ok(rows)
    }

    /// Top recurring groups for one dimension.
    pub fn top_groups(
        &self,
        filter: &QueryFilter,
        by: GroupBy,
        limit: usize,
    ) -> Result<Vec<(String, i64)>, StorageError> {
        self.storage.top_groups(filter, by, limit)
    }

    /// Recurring failures, newest first.
    pub fn error_groups(
        &self,
        filter: &QueryFilter,
        limit: usize,
    ) -> Result<Vec<query::ErrorAgg>, StorageError> {
        self.storage.error_aggregates(filter, limit)
    }

    /// Time-bucketed counts.
    pub fn buckets(
        &self,
        filter: &QueryFilter,
        bucket_secs: i64,
        max_buckets: usize,
    ) -> Result<Vec<query::Bucket>, StorageError> {
        self.storage.time_buckets(filter, bucket_secs, max_buckets)
    }

    /// Bounded-memory export: walks the match set in keyset chunks and returns at
    /// most `max_rows` records. The engine never materialises "all matching rows";
    /// this is the supported way to hand a large result to an exporter (backup,
    /// migration to PostgreSQL).
    pub fn export(
        &self,
        filter: QueryFilter,
        chunk: usize,
        max_rows: u64,
    ) -> Result<Vec<Record>, StorageError> {
        let mut exporter = Exporter::new(filter, chunk, max_rows, false);
        let mut out: Vec<Record> = Vec::new();
        while let Some(page) = exporter.next_page() {
            let rows = self.storage.export_chunk(exporter.filter(), &page)?;
            let more = exporter.accept(&rows);
            out.extend(rows);
            if !more {
                break;
            }
        }
        Ok(out)
    }

    fn to_proto(record: &Record) -> pb::LogEntry {
        pb::LogEntry {
            ts: record.ts,
            level: record.level.clone(),
            service: record.service.clone(),
            category: record.category.clone(),
            message: record.message.clone(),
            detail: record.detail.clone(),
            rule_id: record.rule_id.clone(),
            user_id: record.user_id,
            correlation_id: record.correlation_id.clone(),
        }
    }

    fn clamp_i32(value: i64) -> i32 {
        value.clamp(i32::MIN as i64, i32::MAX as i64) as i32
    }
}

/// Per-RPC bookkeeping: duration, status, in-flight and cancellation.
///
/// A dropped future means the client went away — that is a cancellation, and
/// counting it (instead of losing the RPC entirely) is the difference between "we
/// never saw that request" and "the client hung up mid-request".
struct RpcGuard {
    metrics: Arc<Metrics>,
    method: &'static str,
    started: Instant,
    finished: AtomicBool,
}

impl RpcGuard {
    fn start(metrics: &Arc<Metrics>, method: &'static str) -> Self {
        metrics.rpc_in_flight.inc();
        RpcGuard {
            metrics: Arc::clone(metrics),
            method,
            started: Instant::now(),
            finished: AtomicBool::new(false),
        }
    }

    fn finish(&self, status_code: i32) {
        if self.finished.swap(true, Ordering::SeqCst) {
            return;
        }
        self.metrics.rpc_in_flight.dec();
        self.metrics
            .record_rpc(self.method, status_code, self.started.elapsed());
    }
}

impl Drop for RpcGuard {
    fn drop(&mut self) {
        if !self.finished.load(Ordering::SeqCst) {
            self.metrics.rpc_cancelled.inc();
            self.metrics.rpc_in_flight.dec();
            self.metrics
                .record_rpc(self.method, 1 /* CANCELLED */, self.started.elapsed());
        }
    }
}

#[tonic::async_trait]
impl pb::log_control_service_server::LogControlService for LogControl {
    async fn log(
        &self,
        request: tonic::Request<pb::LogRequest>,
    ) -> Result<tonic::Response<pb::StatusResponse>, tonic::Status> {
        let guard = RpcGuard::start(&self.metrics, "Log");
        self.metrics.events_received.inc();

        let entry = request.into_inner().entry;
        let Some(entry) = entry else {
            guard.finish(3);
            let mut response = self.reject("INVALID", None, false);
            response.message = "LogRequest.entry is required".to_string();
            return Ok(tonic::Response::new(response));
        };
        let raw = RawEntry::from(&entry);
        let prepared = match ingest::prepare(
            &raw,
            self.limits,
            self.redact_identifiers,
            &self.policy,
            self.ts_unit.now(),
        ) {
            Ok(prepared) => prepared,
            Err(reject) => {
                let filtered = matches!(reject, Reject::Filtered { .. });
                let level = match &reject {
                    Reject::Filtered { level, .. } => Some(level.clone()),
                    Reject::Invalid(_) => None,
                };
                guard.finish(0);
                let mut response = self.reject(reject.code(), level.as_deref(), filtered);
                response.message = reject.message();
                return Ok(tonic::Response::new(response));
            }
        };

        let ack = match self.admit_with_backpressure(prepared).await {
            Ok(ack) => ack,
            Err(err) => {
                guard.finish(8); // RESOURCE_EXHAUSTED
                let mut response = self.reject(err.code(), None, false);
                response.message = err.message();
                return Ok(tonic::Response::new(response));
            }
        };

        match ack {
            Ack::Immediate => {
                // Queued mode: the record is in the bounded queue. The response says
                // exactly that; readiness carries the weak-durability warning.
                guard.finish(0);
                Ok(tonic::Response::new(self.status(
                    true,
                    "",
                    "queued for persistence (ack-mode=queued)",
                )))
            }
            Ack::Pending(receiver) => {
                self.metrics.in_flight_acks.inc();
                let result = tokio::time::timeout(self.ack_timeout, receiver).await;
                self.metrics.in_flight_acks.dec();
                match result {
                    Ok(Ok(Ok(()))) => {
                        guard.finish(0);
                        Ok(tonic::Response::new(self.status(
                            true,
                            "",
                            "persisted (committed to storage)",
                        )))
                    }
                    Ok(Ok(Err(storage_error))) => {
                        guard.finish(13); // INTERNAL
                        let mut response = self.reject(storage_error.code(), None, false);
                        response.message = format!(
                            "record was not persisted: {}",
                            storage_error.message()
                        );
                        Ok(tonic::Response::new(response))
                    }
                    Ok(Err(_recv)) => {
                        // The writer dropped the ack channel: it never reported a
                        // commit, so this is a failure, not a success.
                        guard.finish(13);
                        let mut response = self.reject(CODE_DROPPED, None, false);
                        response.message =
                            "record was dropped before storage confirmed the write".to_string();
                        Ok(tonic::Response::new(response))
                    }
                    Err(_elapsed) => {
                        self.metrics.rpc_timeouts.inc();
                        guard.finish(4); // DEADLINE_EXCEEDED
                        let mut response = self.reject("TIMEOUT", None, false);
                        response.message = format!(
                            "storage did not confirm the write within {} ms; it may still be persisted",
                            self.ack_timeout.as_millis()
                        );
                        Ok(tonic::Response::new(response))
                    }
                }
            }
        }
    }

    async fn query_logs(
        &self,
        request: tonic::Request<pb::QueryLogsRequest>,
    ) -> Result<tonic::Response<pb::QueryLogsResponse>, tonic::Status> {
        let guard = RpcGuard::start(&self.metrics, "QueryLogs");
        let request = request.into_inner();
        let filter = QueryFilter {
            levels: request.levels,
            level: request.level,
            service: request.service,
            category: request.category,
            search: request.search,
            rule_id: request.rule_id,
            correlation_id: request.correlation_id,
            user_id: request.user_id,
            since: request.since,
            until: request.until,
        };
        let page = Page::clamped(request.limit, request.offset, request.ascending);
        let storage = Arc::clone(&self.storage);
        let join = tokio::task::spawn_blocking(move || storage.query(&filter, &page)).await;
        match join {
            Ok(Ok((rows, total))) => {
                let logs: Vec<pb::LogEntry> = rows.iter().map(Self::to_proto).collect();
                let has_more = ((page.offset + logs.len()) as i64) < total;
                guard.finish(0);
                Ok(tonic::Response::new(pb::QueryLogsResponse {
                    logs,
                    total: Self::clamp_i32(total),
                    has_more,
                }))
            }
            Ok(Err(err)) => {
                guard.finish(13);
                self.metrics.storage_transaction_failures.inc();
                // A read failure is a broken storage, not a slow one: readiness must
                // stop advertising this logger as able to serve durable logging.
                self.health
                    .mark_not_ready(crate::health::REASON_STORAGE_UNAVAILABLE, err.to_string());
                Err(tonic::Status::internal(format!(
                    "query failed: {}",
                    err.code()
                )))
            }
            Err(join_error) => {
                guard.finish(13);
                Err(tonic::Status::internal(format!(
                    "query task failed: {join_error}"
                )))
            }
        }
    }

    async fn log_stats(
        &self,
        request: tonic::Request<pb::LogStatsRequest>,
    ) -> Result<tonic::Response<pb::LogStatsResponse>, tonic::Status> {
        let guard = RpcGuard::start(&self.metrics, "LogStats");
        let request = request.into_inner();
        let scope = StatsScope {
            rule_id: request.rule_id,
            user_id: request.user_id,
        };
        let storage = Arc::clone(&self.storage);
        let join = tokio::task::spawn_blocking(move || storage.stats(&scope)).await;
        match join {
            Ok(Ok(stats)) => {
                let to_map = |source: &std::collections::BTreeMap<String, i64>| {
                    source
                        .iter()
                        .map(|(key, value)| (key.clone(), Self::clamp_i32(*value)))
                        .collect::<std::collections::HashMap<String, i32>>()
                };
                guard.finish(0);
                Ok(tonic::Response::new(pb::LogStatsResponse {
                    total: stats.total,
                    by_level: to_map(&stats.by_level),
                    by_category: to_map(&stats.by_category),
                    by_rule: to_map(&stats.by_rule),
                    by_service: to_map(&stats.by_service),
                    oldest_ts: stats.oldest_ts,
                    newest_ts: stats.newest_ts,
                }))
            }
            Ok(Err(err)) => {
                guard.finish(13);
                self.metrics.storage_transaction_failures.inc();
                self.health
                    .mark_not_ready(crate::health::REASON_STORAGE_UNAVAILABLE, err.to_string());
                Err(tonic::Status::internal(format!(
                    "stats failed: {}",
                    err.code()
                )))
            }
            Err(join_error) => {
                guard.finish(13);
                Err(tonic::Status::internal(format!(
                    "stats task failed: {join_error}"
                )))
            }
        }
    }

    async fn purge_logs(
        &self,
        request: tonic::Request<pb::PurgeLogsRequest>,
    ) -> Result<tonic::Response<pb::PurgeLogsResponse>, tonic::Status> {
        let guard = RpcGuard::start(&self.metrics, "PurgeLogs");
        let request = request.into_inner();
        let category = request.category.trim().to_string();
        // An explicit request to delete an audit/security category is refused
        // rather than silently doing nothing: the policy protects those rows, and
        // an operator needs to be told that, not left guessing why "0 purged".
        if !category.is_empty()
            && self
                .protected_categories
                .iter()
                .any(|c| c.trim().eq_ignore_ascii_case(category.trim()))
        {
            guard.finish(0);
            return Ok(tonic::Response::new(pb::PurgeLogsResponse {
                success: false,
                purged: 0,
                remaining: self.storage.table_rows().unwrap_or(0),
                error: format!(
                    "PROTECTED_CATEGORY: {category} is protected by the retention policy \
                     (ATF_LOGGER_PROTECTED_CATEGORIES); audit/security events are not purged \
                     by request"
                ),
            }));
        }
        let filter = PurgeFilter {
            level: request.level,
            service: request.service,
            category,
            rule_id: request.rule_id,
            older_than_hours: request.older_than_hours.max(0) as u64,
            protect_categories: self.protected_categories.clone(),
            now: self.ts_unit.now(),
        };
        let storage = Arc::clone(&self.storage);
        let chunk = query::MAX_PAGE;
        let join = tokio::task::spawn_blocking(move || {
            storage.purge(&filter, chunk, MAX_PURGE_ROWS)
        });
        let result = tokio::time::timeout(self.purge_timeout, join).await;
        match result {
            Ok(Ok(Ok(report))) => {
                guard.finish(0);
                Ok(tonic::Response::new(Self::purge_response(report)))
            }
            Ok(Ok(Err(err))) => {
                guard.finish(13);
                Ok(tonic::Response::new(pb::PurgeLogsResponse {
                    success: false,
                    purged: 0,
                    remaining: 0,
                    error: format!("{}: {}", err.code(), err.message()),
                }))
            }
            Ok(Err(join_error)) => {
                guard.finish(13);
                Ok(tonic::Response::new(pb::PurgeLogsResponse {
                    success: false,
                    purged: 0,
                    remaining: 0,
                    error: format!("purge task failed: {join_error}"),
                }))
            }
            Err(_elapsed) => {
                self.metrics.rpc_timeouts.inc();
                guard.finish(4);
                Ok(tonic::Response::new(pb::PurgeLogsResponse {
                    success: false,
                    purged: 0,
                    remaining: 0,
                    error: format!(
                        "TIMEOUT: purge exceeded {} ms; it may have deleted some rows",
                        self.purge_timeout.as_millis()
                    ),
                }))
            }
        }
    }
}

impl LogControl {
    /// The `PurgeLogsResponse` for a completed purge, with the honest notes: what
    /// was deleted, what is left, whether the row cap stopped it, and whether
    /// protected rows were spared.
    fn purge_response(report: PurgeReport) -> pb::PurgeLogsResponse {
        let mut notes = Vec::new();
        if report.exhausted {
            notes.push(format!(
                "row cap reached ({MAX_PURGE_ROWS}); call again to continue"
            ));
        }
        if report.protected_skipped {
            notes.push("protected categories were spared".to_string());
        }
        let error = if notes.is_empty() {
            String::new()
        } else {
            notes.join("; ")
        };
        pb::PurgeLogsResponse {
            success: true,
            purged: report.purged,
            remaining: report.remaining,
            error,
        }
    }

    /// The durable-ack warning the health state carries in queued mode.
    pub fn mark_ack_mode(&self, health: &HealthState) {
        if self.ack_mode == "queued" {
            health.mark_degraded(
                REASON_ACK_MODE_QUEUED,
                "ack-mode=queued: success means enqueued, not committed",
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{AckMode, Config};
    use crate::pipeline::PipelineConfig;
    use crate::storage::sqlite::SqliteStorage;
    use crate::storage::BackendConfig;
    use crate::storage::Backend;

    struct Harness {
        _dir: tempfile::TempDir,
        storage: Arc<dyn Storage>,
        metrics: Arc<Metrics>,
        health: Arc<HealthState>,
        pipeline: Arc<Pipeline>,
        service: LogControl,
    }

    fn harness(config: Config) -> Harness {
        let dir = tempfile::tempdir().unwrap();
        let metrics = Arc::new(Metrics::new());
        let backend = BackendConfig {
            backend: Backend::Sqlite,
            sqlite_path: dir.path().join("transport.db").to_string_lossy().to_string(),
            postgres_dsn: None,
            busy_timeout_ms: 5_000,
            read_pool_size: 2,
            pool_timeout_ms: 5_000,
            statement_timeout_ms: 0,
            transaction_timeout_ms: 0,
            max_rows_per_statement: 500,
        };
        let storage: Arc<dyn Storage> = Arc::new(
            SqliteStorage::open(&config, &backend, Arc::clone(&metrics)).unwrap(),
        );
        let pipeline = Pipeline::start(
            PipelineConfig {
                ack_mode: AckMode::Durable,
                batch_size: 32,
                ..PipelineConfig::from_config(&config)
            },
            Arc::clone(&storage),
            Arc::clone(&metrics),
        )
        .unwrap();
        let health = Arc::new(HealthState::new());
        health.mark_ready("test storage probe ok");
        let policy = CollectPolicy::permissive();
        let service = LogControl::new(
            &config,
            policy.clone(),
            Arc::clone(&pipeline),
            Arc::clone(&storage),
            Arc::clone(&metrics),
            Arc::clone(&health),
        )
        .with_backend("sqlite(test)");
        Harness {
            _dir: dir,
            storage,
            metrics,
            health,
            pipeline,
            service,
        }
    }

    fn entry(level: &str, message: &str) -> pb::LogEntry {
        pb::LogEntry {
            ts: 1_700_000_000,
            level: level.to_string(),
            service: "core".to_string(),
            category: "system".to_string(),
            message: message.to_string(),
            detail: "{}".to_string(),
            rule_id: String::new(),
            user_id: 0,
            correlation_id: String::new(),
        }
    }

    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap()
    }

    #[test]
    fn log_acks_only_after_commit_and_never_returns_a_secret() {
        let harness = harness(Config::default());
        let runtime = runtime();
        let response = runtime
            .block_on(
                pb::log_control_service_server::LogControlService::log(
                    &harness.service,
                    tonic::Request::new(pb::LogRequest {
                        entry: Some(pb::LogEntry {
                            message: "login failed for Authorization: Bearer SECRET-TOKEN-XYZ"
                                .to_string(),
                            detail: r#"{"password":"hunter2","nested":{"token":"abc12345"}}"#
                                .to_string(),
                            ..entry("ERROR", "unused")
                        }),
                    }),
                ),
            )
            .unwrap()
            .into_inner();
        assert!(response.success, "{response:?}");
        assert_eq!(response.error_code, "");
        // Durable mode: the write is committed by the time the ack says so.
        assert_eq!(harness.storage.table_rows().unwrap(), 1);
        let rows = harness.service.recent(&QueryFilter::default(), 10).unwrap();
        assert_eq!(rows.len(), 1);
        assert!(!rows[0].message.contains("SECRET-TOKEN-XYZ"), "{}", rows[0].message);
        assert!(!rows[0].detail.contains("hunter2"), "{}", rows[0].detail);
        assert!(!rows[0].detail.contains("abc12345"), "{}", rows[0].detail);
        assert!(rows[0].message.contains("[REDACTED]"), "{}", rows[0].message);
        assert_eq!(harness.metrics.events_received.get(), 1);
        assert_eq!(harness.metrics.events_accepted.with("ERROR").get(), 1);
        assert_eq!(harness.metrics.events_persisted.get(), 1);
        assert_eq!(harness.metrics.rpc_total.with("Log").get(), 1);
        harness.pipeline.stop(Duration::from_secs(5));
    }

    #[test]
    fn missing_entry_is_invalid_not_a_crash() {
        let harness = harness(Config::default());
        let runtime = runtime();
        let response = runtime
            .block_on(
                pb::log_control_service_server::LogControlService::log(
                    &harness.service,
                    tonic::Request::new(pb::LogRequest { entry: None }),
                ),
            )
            .unwrap()
            .into_inner();
        assert!(!response.success);
        assert_eq!(response.error_code, "INVALID");
        assert_eq!(harness.metrics.events_rejected.with("invalid").get(), 1);
        harness.pipeline.stop(Duration::from_secs(5));
    }

    #[test]
    fn default_policy_filters_info_but_counts_it() {
        // The contract's default collection policy, not the permissive test one.
        let config = Config::default();
        let dir = tempfile::tempdir().unwrap();
        let metrics = Arc::new(Metrics::new());
        let backend = BackendConfig {
            backend: Backend::Sqlite,
            sqlite_path: dir.path().join("policy.db").to_string_lossy().to_string(),
            postgres_dsn: None,
            busy_timeout_ms: 5_000,
            read_pool_size: 2,
            pool_timeout_ms: 5_000,
            statement_timeout_ms: 0,
            transaction_timeout_ms: 0,
            max_rows_per_statement: 500,
        };
        let storage: Arc<dyn Storage> = Arc::new(
            SqliteStorage::open(&config, &backend, Arc::clone(&metrics)).unwrap(),
        );
        let pipeline = Pipeline::start(
            PipelineConfig::from_config(&config),
            Arc::clone(&storage),
            Arc::clone(&metrics),
        )
        .unwrap();
        let health = Arc::new(HealthState::new());
        let policy = CollectPolicy::contract_default(&config.protected_categories);
        let service = LogControl::new(
            &config,
            policy,
            Arc::clone(&pipeline),
            Arc::clone(&storage),
            Arc::clone(&metrics),
            health,
        );
        let runtime = runtime();
        let filtered = runtime
            .block_on(
                pb::log_control_service_server::LogControlService::log(
                    &service,
                    tonic::Request::new(pb::LogRequest {
                        entry: Some(entry("INFO", "chatty")),
                    }),
                ),
            )
            .unwrap()
            .into_inner();
        assert!(!filtered.success);
        assert_eq!(filtered.error_code, "FILTERED");
        assert!(filtered.message.contains("ATF_LOGGER_COLLECT_LEVELS"));
        let kept = runtime
            .block_on(
                pb::log_control_service_server::LogControlService::log(
                    &service,
                    tonic::Request::new(pb::LogRequest {
                        entry: Some(entry("ERROR", "important")),
                    }),
                ),
            )
            .unwrap()
            .into_inner();
        assert!(kept.success, "{kept:?}");
        // Audit categories bypass the threshold even at INFO.
        let audit = runtime
            .block_on(
                pb::log_control_service_server::LogControlService::log(
                    &service,
                    tonic::Request::new(pb::LogRequest {
                        entry: Some(pb::LogEntry {
                            category: "audit".to_string(),
                            ..entry("INFO", "operator login")
                        }),
                    }),
                ),
            )
            .unwrap()
            .into_inner();
        assert!(audit.success, "{audit:?}");
        assert_eq!(metrics.events_filtered.with("INFO").get(), 1);
        assert_eq!(storage.table_rows().unwrap(), 2);
        pipeline.stop(Duration::from_secs(5));
    }

    #[test]
    fn query_logs_mirrors_the_python_filter_semantics() {
        let harness = harness(Config::default());
        let runtime = runtime();
        let mut entries = Vec::new();
        for index in 0..25 {
            entries.push(pb::LogEntry {
                ts: 1_700_000_000 + index,
                level: if index % 5 == 0 { "ERROR" } else { "WARN" }.to_string(),
                service: "core".to_string(),
                category: if index % 2 == 0 { "system" } else { "payment" }.to_string(),
                message: format!("row {index} needle"),
                detail: "{}".to_string(),
                rule_id: String::new(),
                user_id: if index < 10 { 7 } else { 8 },
                correlation_id: format!("corr-{index}"),
            });
        }
        for entry in entries {
            let response = runtime
                .block_on(
                    pb::log_control_service_server::LogControlService::log(
                        &harness.service,
                        tonic::Request::new(pb::LogRequest { entry: Some(entry) }),
                    ),
                )
                .unwrap()
                .into_inner();
            assert!(response.success, "{response:?}");
        }

        let query = |request: pb::QueryLogsRequest| {
            runtime
                .block_on(
                    pb::log_control_service_server::LogControlService::query_logs(
                        &harness.service,
                        tonic::Request::new(request),
                    ),
                )
                .unwrap()
                .into_inner()
        };

        // Page one: newest first, with a stable total.
        let page = query(pb::QueryLogsRequest {
            limit: 10,
            ..Default::default()
        });
        assert_eq!(page.logs.len(), 10);
        assert_eq!(page.total, 25);
        assert!(page.has_more);
        assert_eq!(page.logs[0].ts, 1_700_000_024);
        assert_eq!(page.logs[9].ts, 1_700_000_015);

        // Offset paging continues where the first page stopped.
        let second = query(pb::QueryLogsRequest {
            limit: 10,
            offset: 10,
            ..Default::default()
        });
        assert_eq!(second.logs[0].ts, 1_700_000_014);

        // Ascending order.
        let ascending = query(pb::QueryLogsRequest {
            limit: 3,
            ascending: true,
            ..Default::default()
        });
        assert_eq!(ascending.logs[0].ts, 1_700_000_000);

        // Multi-select levels win over the single level.
        let errors = query(pb::QueryLogsRequest {
            levels: vec!["ERROR".to_string()],
            level: "WARN".to_string(),
            limit: 100,
            ..Default::default()
        });
        assert_eq!(errors.total, 5);
        assert!(errors.logs.iter().all(|l| l.level == "ERROR"));

        // Tenant scoping.
        let tenant = query(pb::QueryLogsRequest {
            user_id: 7,
            limit: 100,
            ..Default::default()
        });
        assert_eq!(tenant.total, 10);

        // Search, category and correlation filters.
        let searched = query(pb::QueryLogsRequest {
            search: "needle".to_string(),
            limit: 100,
            ..Default::default()
        });
        assert_eq!(searched.total, 25);
        let payment = query(pb::QueryLogsRequest {
            category: "payment".to_string(),
            limit: 100,
            ..Default::default()
        });
        assert_eq!(payment.total, 12);
        let correlated = query(pb::QueryLogsRequest {
            correlation_id: "corr-3".to_string(),
            limit: 100,
            ..Default::default()
        });
        assert_eq!(correlated.total, 1);
        assert_eq!(correlated.logs[0].correlation_id, "corr-3");

        // Time range.
        let window = query(pb::QueryLogsRequest {
            since: 1_700_000_010,
            until: 1_700_000_014,
            limit: 100,
            ..Default::default()
        });
        assert_eq!(window.total, 5);

        // The limit is clamped, not trusted.
        let clamped = query(pb::QueryLogsRequest {
            limit: 100_000,
            ..Default::default()
        });
        assert_eq!(clamped.logs.len(), 25);
        assert_eq!(harness.metrics.rpc_total.with("QueryLogs").get(), 10);
        harness.pipeline.stop(Duration::from_secs(5));
    }

    #[test]
    fn log_stats_reports_totals_groups_and_window() {
        let harness = harness(Config::default());
        let runtime = runtime();
        for index in 0..12 {
            let level = match index % 3 {
                0 => "ERROR",
                1 => "WARN",
                _ => "CRITICAL",
            };
            runtime
                .block_on(
                    pb::log_control_service_server::LogControlService::log(
                        &harness.service,
                        tonic::Request::new(pb::LogRequest {
                            entry: Some(pb::LogEntry {
                                rule_id: if index % 2 == 0 { "rule-a" } else { "rule-b" }.to_string(),
                                user_id: if index % 2 == 0 { 5 } else { 6 },
                                ..entry(level, &format!("stats {index}"))
                            }),
                        }),
                    ),
                )
                .unwrap();
        }
        let stats = runtime
            .block_on(
                pb::log_control_service_server::LogControlService::log_stats(
                    &harness.service,
                    tonic::Request::new(pb::LogStatsRequest {
                        rule_id: String::new(),
                        user_id: 0,
                    }),
                ),
            )
            .unwrap()
            .into_inner();
        assert_eq!(stats.total, 12);
        assert_eq!(stats.by_level.get("ERROR"), Some(&4));
        assert_eq!(stats.by_level.get("WARN"), Some(&4));
        assert_eq!(stats.by_level.get("CRITICAL"), Some(&4));
        assert_eq!(stats.by_category.get("system"), Some(&12));
        // `by_rule` covers the whole archive regardless of scope, like Python.
        assert_eq!(stats.by_rule.get("rule-a"), Some(&6));
        assert_eq!(stats.by_rule.get("rule-b"), Some(&6));
        assert_eq!(stats.oldest_ts, 1_700_000_000);
        assert_eq!(stats.newest_ts, 1_700_000_000);

        // Scoped stats: only one rule's rows, but the same by_rule map.
        let scoped = runtime
            .block_on(
                pb::log_control_service_server::LogControlService::log_stats(
                    &harness.service,
                    tonic::Request::new(pb::LogStatsRequest {
                        rule_id: "rule-a".to_string(),
                        user_id: 0,
                    }),
                ),
            )
            .unwrap()
            .into_inner();
        assert_eq!(scoped.total, 6);
        assert_eq!(scoped.by_rule.get("rule-b"), Some(&6));
        harness.pipeline.stop(Duration::from_secs(5));
    }

    #[test]
    fn purge_logs_respects_time_scope_and_protected_categories() {
        let harness = harness(Config::default());
        let runtime = runtime();
        let now = TsUnit::Seconds.now();
        for (index, (age_hours, category)) in [
            (48u64, "system"),
            (48, "system"),
            (1, "system"),
            (48, "audit"),
        ]
        .into_iter()
        .enumerate()
        {
            let response = runtime
                .block_on(
                    pb::log_control_service_server::LogControlService::log(
                        &harness.service,
                        tonic::Request::new(pb::LogRequest {
                            entry: Some(pb::LogEntry {
                                ts: now - (age_hours as i64) * 3600,
                                category: category.to_string(),
                                ..entry("ERROR", &format!("purge {index}"))
                            }),
                        }),
                    ),
                )
                .unwrap()
                .into_inner();
            assert!(response.success, "{response:?}");
        }
        assert_eq!(harness.storage.table_rows().unwrap(), 4);

        // 24h scope: the two 48h-old SYSTEM rows go, the fresh one stays, and the
        // old AUDIT row is spared even though its age qualifies.
        let purged = runtime
            .block_on(
                pb::log_control_service_server::LogControlService::purge_logs(
                    &harness.service,
                    tonic::Request::new(pb::PurgeLogsRequest {
                        older_than_hours: 24,
                        ..Default::default()
                    }),
                ),
            )
            .unwrap()
            .into_inner();
        assert!(purged.success, "{purged:?}");
        assert_eq!(purged.purged, 2);
        assert_eq!(purged.remaining, 2);
        assert!(purged.error.contains("protected categories were spared"), "{purged:?}");
        assert_eq!(harness.storage.table_rows().unwrap(), 2);

        // An explicit purge of a protected category is refused, not silently empty.
        let refused = runtime
            .block_on(
                pb::log_control_service_server::LogControlService::purge_logs(
                    &harness.service,
                    tonic::Request::new(pb::PurgeLogsRequest {
                        category: "AUDIT".to_string(),
                        older_than_hours: 24,
                        ..Default::default()
                    }),
                ),
            )
            .unwrap()
            .into_inner();
        assert!(!refused.success);
        assert!(refused.error.contains("PROTECTED_CATEGORY"), "{refused:?}");
        assert_eq!(harness.storage.table_rows().unwrap(), 2);
        harness.pipeline.stop(Duration::from_secs(5));
    }

    #[test]
    fn query_helpers_cover_groups_buckets_and_bounded_export() {
        let harness = harness(Config::default());
        for index in 0..30 {
            let response = harness
                .pipeline
                .admit(
                    ingest::prepare(
                        &RawEntry {
                            ts: 1_700_000_000 + index,
                            level: if index % 2 == 0 { "ERROR" } else { "WARN" }.to_string(),
                            service: "core".to_string(),
                            category: "system".to_string(),
                            message: "repeated failure".to_string(),
                            detail: "{}".to_string(),
                            rule_id: String::new(),
                            user_id: 0,
                            correlation_id: String::new(),
                        },
                        Limits::from_config(&Config::default()),
                        true,
                        &CollectPolicy::permissive(),
                        1_700_000_000,
                    )
                    .unwrap(),
                )
                .map(|_| ())
                .map_err(|err| err.code().to_string());
            assert!(response.is_ok(), "{response:?}");
        }
        harness.pipeline.stop(Duration::from_secs(5));

        let filter = QueryFilter::default();
        let groups = harness.service.top_groups(&filter, GroupBy::Level, 10).unwrap();
        assert!(groups.iter().any(|(key, count)| key == "ERROR" && *count == 15));
        let buckets = harness.service.buckets(&filter, 10, 100).unwrap();
        assert_eq!(buckets.iter().map(|b| b.count).sum::<i64>(), 30);
        let errors = harness.service.error_groups(&filter, 10).unwrap();
        // Identical messages collapse into one recurring group.
        assert_eq!(errors.len(), 1);
        assert_eq!(errors[0].count, 30);
        let recent = harness.service.recent(&filter, 5).unwrap();
        assert_eq!(recent.len(), 5);
        assert_eq!(recent[0].ts, 1_700_000_029);
        // Bounded export caps at the requested ceiling.
        let exported = harness.service.export(filter, 7, 20).unwrap();
        assert!(exported.len() <= 20);
    }

    #[test]
    fn cancelled_rpc_is_counted() {
        let metrics = Arc::new(Metrics::new());
        {
            let _guard = RpcGuard::start(&metrics, "Log");
            assert_eq!(metrics.rpc_in_flight.get(), 1);
            // Dropped without finishing: a client that hung up.
        }
        assert_eq!(metrics.rpc_cancelled.get(), 1);
        assert_eq!(metrics.rpc_in_flight.get(), 0);
        assert_eq!(metrics.rpc_total.with("Log").get(), 1);
        {
            let guard = RpcGuard::start(&metrics, "Log");
            guard.finish(0);
            guard.finish(0);
        }
        assert_eq!(metrics.rpc_cancelled.get(), 1, "a finished RPC is not cancelled");
        assert_eq!(metrics.rpc_total.with("Log").get(), 2);
    }

    #[test]
    fn queued_ack_mode_reports_degraded_readiness() {
        let mut config = Config::default();
        config.ack_mode = AckMode::Queued;
        let harness = harness(config);
        let health = HealthState::new();
        harness.service.mark_ack_mode(&health);
        assert_eq!(health.reason_code(), crate::health::REASON_ACK_MODE_QUEUED);
        assert!(health.is_ready(), "queued mode still serves requests");
        assert_eq!(harness.service.ack_mode(), "queued");
        harness.pipeline.stop(Duration::from_secs(5));
    }

    #[test]
    fn overload_is_refused_with_overload_and_counted() {
        let mut config = Config::default();
        config.queue_capacity = 2;
        config.priority_capacity = 1;
        config.batch_delay_ms = 5_000;
        let harness = harness(config);
        let runtime = runtime();
        let mut refused = 0;
        for index in 0..40 {
            let response = runtime
                .block_on(
                    pb::log_control_service_server::LogControlService::log(
                        &harness.service,
                        tonic::Request::new(pb::LogRequest {
                            entry: Some(entry("ERROR", &format!("flood {index}"))),
                        }),
                    ),
                )
                .unwrap()
                .into_inner();
            if !response.success {
                assert_eq!(response.error_code, "OVERLOAD");
                refused += 1;
            }
        }
        assert!(refused > 0, "a 3-slot pipeline must refuse a 40-record flood");
        assert_eq!(harness.metrics.events_rejected.with("overload").get(), refused);
        assert!(harness.metrics.queue_rejected_full.total() >= refused);
        harness.pipeline.stop(Duration::from_secs(10));
    }
}
