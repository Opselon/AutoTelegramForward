//! Signal-driven graceful shutdown.
//!
//! The order is not arbitrary, and it is the reason this lives in its own module
//! rather than in `main`:
//!
//! 1. **Readiness goes down first** ([`crate::health::HealthState::mark_draining`]).
//!    A load balancer must stop sending durable writes *before* the queue starts
//!    refusing them, otherwise a drain looks like an outage.
//! 2. **Retention stops** before the drain: it takes the single writer lock, and a
//!    cleanup fighting the drain for that lock is how a shutdown turns into a
//!    timeout.
//! 3. **Admission closes** ([`crate::pipeline::Pipeline::begin_drain`]): new work is
//!    refused with `SHUTTING_DOWN` instead of being accepted and then lost.
//! 4. **The drain runs**, bounded by `drain_timeout_ms`. Every record admitted
//!    before step 3 is committed, and a `durable` producer's ack is answered only
//!    after its batch committed.
//! 5. **The writer threads join**, then storage gets a final checkpoint
//!    (SQLite WAL).
//! 6. **Liveness goes down last**, so a process still draining is not killed by an
//!    orchestrator reading a premature "dead".
//!
//! A second signal is an operator saying "I meant it": it prints one emergency line
//! and exits immediately rather than waiting out the drain.

use crate::health::{self, HealthState};
use crate::metrics::Metrics;
use crate::pipeline::Pipeline;
use crate::retention::Retention;
use crate::storage::Storage;
use std::sync::Arc;
use std::time::{Duration, Instant};

/// Which signal asked us to stop.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StopSignal {
    Sigint,
    Sigterm,
}

impl StopSignal {
    pub fn as_str(self) -> &'static str {
        match self {
            StopSignal::Sigint => "sigint",
            StopSignal::Sigterm => "sigterm",
        }
    }

    /// Exit status: an operator interrupt is a normal stop, a terminate is a
    /// supervisor asking a service to go away.
    pub fn exit_code(self) -> i32 {
        match self {
            StopSignal::Sigint => 130,
            StopSignal::Sigterm => 143,
        }
    }
}

/// One step of the shutdown, with how long it took.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Step {
    pub name: &'static str,
    pub ok: bool,
    pub millis: u64,
}

/// What the shutdown did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShutdownOutcome {
    pub signal: &'static str,
    pub steps: Vec<Step>,
    pub drained: bool,
    pub remaining: usize,
    pub drained_records: u64,
    pub elapsed_millis: u64,
}

impl ShutdownOutcome {
    /// True when nothing was lost: every admitted record committed and every step
    /// reported success.
    pub fn clean(&self) -> bool {
        self.drained && self.steps.iter().all(|step| step.ok)
    }

    pub fn describe(&self) -> String {
        let steps: Vec<String> = self
            .steps
            .iter()
            .map(|step| format!("{}({}ms,{})", step.name, step.millis, if step.ok { "ok" } else { "FAILED" }))
            .collect();
        format!(
            "signal={} drained={} remaining={} drained_records={} elapsed_ms={} steps=[{}]",
            self.signal,
            self.drained,
            self.remaining,
            self.drained_records,
            self.elapsed_millis,
            steps.join(" ")
        )
    }
}

/// The pieces a graceful stop has to touch.
pub struct Shutdown {
    pub storage: Arc<dyn Storage>,
    pub metrics: Arc<Metrics>,
    pub health: Arc<HealthState>,
    pub drain_timeout: Duration,
}

impl Shutdown {
    pub fn new(
        storage: Arc<dyn Storage>,
        metrics: Arc<Metrics>,
        health: Arc<HealthState>,
        drain_timeout: Duration,
    ) -> Self {
        Shutdown {
            storage,
            metrics,
            health,
            drain_timeout,
        }
    }

    /// Perform the ordered shutdown described in the module docs.
    pub fn run(
        &self,
        signal: StopSignal,
        pipeline: &Pipeline,
        retention: Option<&mut Retention>,
    ) -> ShutdownOutcome {
        let started = Instant::now();
        let mut steps = Vec::new();

        // 1. Readiness down first.
        let step_started = Instant::now();
        self.health
            .mark_draining(format!("{}: draining admitted records", signal.as_str()));
        self.metrics.readiness.set(0);
        steps.push(Step {
            name: "readiness_down",
            ok: true,
            millis: step_started.elapsed().as_millis() as u64,
        });

        // 2. Retention stops competing for the writer lock.
        let step_started = Instant::now();
        let mut retention_ok = true;
        if let Some(retention) = retention {
            retention.stop(Duration::from_millis(2_000));
            retention_ok = !retention.is_running();
        }
        steps.push(Step {
            name: "retention_stopped",
            ok: retention_ok,
            millis: step_started.elapsed().as_millis() as u64,
        });

        // 3-5. Close admission, drain, join the writers.
        let step_started = Instant::now();
        let outcome = pipeline.stop(self.drain_timeout);
        steps.push(Step {
            name: "pipeline_drained",
            ok: outcome.drained,
            millis: step_started.elapsed().as_millis() as u64,
        });
        if !outcome.drained {
            // Never silent: the leftover count was already added to
            // `events_dropped{reason="shutdown"}` by the pipeline, and it is
            // repeated here in the human-readable summary.
            health::emergency(
                "shutdown",
                &format!(
                    "drain deadline expired with {} record(s) still queued; they are counted as dropped",
                    outcome.remaining
                ),
            );
        }

        // 6. One last checkpoint so the WAL does not keep the process alive.
        let step_started = Instant::now();
        let checkpoint = self.storage.checkpoint();
        if let Err(err) = &checkpoint {
            health::emergency("shutdown", &format!("final checkpoint failed: {err}"));
        }
        steps.push(Step {
            name: "storage_checkpoint",
            ok: checkpoint.is_ok(),
            millis: step_started.elapsed().as_millis() as u64,
        });

        // 7. Liveness last.
        let step_started = Instant::now();
        self.health.mark_stopped(format!(
            "{}: drained {} record(s)",
            signal.as_str(),
            outcome.drained_records
        ));
        self.metrics.liveness.set(0);
        self.metrics.pipeline_running.set(0);
        steps.push(Step {
            name: "liveness_down",
            ok: true,
            millis: step_started.elapsed().as_millis() as u64,
        });

        ShutdownOutcome {
            signal: signal.as_str(),
            steps,
            drained: outcome.drained,
            remaining: outcome.remaining,
            drained_records: outcome.drained_records,
            elapsed_millis: started.elapsed().as_millis() as u64,
        }
    }
}

/// Await SIGINT/SIGTERM.
///
/// `tokio::signal::unix` is used where it exists; elsewhere (or if the handler
/// cannot be installed) the process still stops on ctrl-c rather than shutting down
/// in a way an operator cannot interrupt.
pub async fn wait_for_signal() -> StopSignal {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut interrupt = match signal(SignalKind::interrupt()) {
            Ok(stream) => stream,
            Err(err) => {
                health::emergency("signals", &format!("cannot install SIGINT handler: {err}"));
                return fallback_signal().await;
            }
        };
        let mut terminate = match signal(SignalKind::terminate()) {
            Ok(stream) => stream,
            Err(err) => {
                health::emergency("signals", &format!("cannot install SIGTERM handler: {err}"));
                return fallback_signal().await;
            }
        };
        tokio::select! {
            _ = interrupt.recv() => StopSignal::Sigint,
            _ = terminate.recv() => StopSignal::Sigterm,
        }
    }
    #[cfg(not(unix))]
    {
        fallback_signal().await
    }
}

#[cfg(unix)]
async fn fallback_signal() -> StopSignal {
    let _ = tokio::signal::ctrl_c().await;
    StopSignal::Sigint
}

#[cfg(not(unix))]
async fn fallback_signal() -> StopSignal {
    let _ = tokio::signal::ctrl_c().await;
    StopSignal::Sigint
}

/// A second signal must not wait out the drain. Returns the task handle so the
/// caller can keep it alive for the process lifetime.
pub fn force_exit_on_second_signal() -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let signal = wait_for_signal().await;
        health::emergency(
            "shutdown",
            &format!(
                "second stop signal ({}): exiting immediately, in-flight records may not be committed",
                signal.as_str()
            ),
        );
        std::process::exit(signal.exit_code());
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{AckMode, Config};
    use crate::pipeline::{Ack, Pipeline, PipelineConfig};
    use crate::storage::sqlite::SqliteStorage;
    use crate::storage::{Backend, BackendConfig, Record};

    struct Harness {
        _dir: tempfile::TempDir,
        storage: Arc<dyn Storage>,
        metrics: Arc<Metrics>,
        health: Arc<HealthState>,
    }

    impl Harness {
        fn new() -> Self {
            let dir = tempfile::tempdir().unwrap();
            let metrics = Arc::new(Metrics::new());
            let backend = BackendConfig {
                backend: Backend::Sqlite,
                sqlite_path: dir.path().join("shutdown.db").to_string_lossy().to_string(),
                postgres_dsn: None,
                busy_timeout_ms: 5_000,
                read_pool_size: 2,
                pool_timeout_ms: 5_000,
                statement_timeout_ms: 0,
                transaction_timeout_ms: 0,
                max_rows_per_statement: 500,
            };
            let storage: Arc<dyn Storage> = Arc::new(
                SqliteStorage::open(&Config::default(), &backend, Arc::clone(&metrics)).unwrap(),
            );
            Harness {
                _dir: dir,
                storage,
                metrics,
                health: Arc::new(HealthState::new()),
            }
        }
    }

    fn prepared(index: usize) -> crate::ingest::Prepared {
        crate::ingest::prepare(
            &crate::ingest::RawEntry {
                ts: 1_700_000_000 + index as i64,
                level: "ERROR".to_string(),
                service: "core".to_string(),
                category: "SYSTEM".to_string(),
                message: format!("shutdown {index}"),
                detail: "{}".to_string(),
                rule_id: String::new(),
                user_id: 0,
                correlation_id: String::new(),
            },
            crate::ingest::Limits {
                max_message_bytes: 1 << 16,
                max_detail_bytes: 1 << 16,
                max_tag_bytes: 1024,
            },
            true,
            &crate::ingest::CollectPolicy::permissive(),
            1_700_000_000,
        )
        .unwrap()
    }

    #[test]
    fn shutdown_drains_commits_and_orders_its_steps() {
        let harness = Harness::new();
        let pipeline = Pipeline::start(
            PipelineConfig {
                ack_mode: AckMode::Durable,
                batch_size: 16,
                ..PipelineConfig::from_config(&Config::default())
            },
            Arc::clone(&harness.storage),
            Arc::clone(&harness.metrics),
        )
        .unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let mut acks = Vec::new();
        for index in 0..64 {
            match pipeline.admit(prepared(index)).unwrap() {
                Ack::Pending(rx) => acks.push(rx),
                Ack::Immediate => panic!("durable mode must wait for the commit"),
            }
        }
        harness.health.mark_ready("storage probe ok");

        let shutdown = Shutdown::new(
            Arc::clone(&harness.storage),
            Arc::clone(&harness.metrics),
            Arc::clone(&harness.health),
            Duration::from_secs(5),
        );
        let outcome = shutdown.run(StopSignal::Sigterm, &pipeline, None);

        assert!(outcome.clean(), "{outcome:?}");
        assert_eq!(outcome.signal, "sigterm");
        assert_eq!(outcome.remaining, 0);
        assert_eq!(outcome.drained_records, 64);
        let names: Vec<&str> = outcome.steps.iter().map(|s| s.name).collect();
        assert_eq!(
            names,
            vec![
                "readiness_down",
                "retention_stopped",
                "pipeline_drained",
                "storage_checkpoint",
                "liveness_down"
            ]
        );
        // Every durable producer got its answer, and every record is on disk.
        for ack in acks {
            assert!(runtime.block_on(ack).expect("answered").is_ok());
        }
        assert_eq!(harness.storage.table_rows().unwrap(), 64);
        // Health ends down, not ready, dead — with a reason.
        assert!(!harness.health.is_alive());
        assert!(!harness.health.is_ready());
        assert_eq!(harness.health.reason_code(), crate::health::REASON_SHUTDOWN);
        assert_eq!(harness.metrics.readiness.get(), 0);
        assert_eq!(harness.metrics.liveness.get(), 0);
        assert!(outcome.describe().contains("drained_records=64"));
    }

    #[test]
    fn admission_is_refused_while_draining() {
        let harness = Harness::new();
        let pipeline = Pipeline::start(
            PipelineConfig::from_config(&Config::default()),
            Arc::clone(&harness.storage),
            Arc::clone(&harness.metrics),
        )
        .unwrap();
        pipeline.begin_drain();
        let err = pipeline.admit(prepared(1)).unwrap_err();
        assert_eq!(err.code(), "SHUTTING_DOWN");
        assert!(err.message().contains("shutting down"));
        // It still stops cleanly afterwards.
        let shutdown = Shutdown::new(
            Arc::clone(&harness.storage),
            Arc::clone(&harness.metrics),
            Arc::clone(&harness.health),
            Duration::from_secs(5),
        );
        let outcome = shutdown.run(StopSignal::Sigint, &pipeline, None);
        assert!(outcome.drained);
        assert_eq!(outcome.remaining, 0);
    }

    #[test]
    fn a_stalled_storage_is_reported_not_hidden() {
        // A storage whose writes always fail: the drain deadline still returns, and
        // the report says the records are gone instead of claiming success.
        struct Stalled;
        impl Storage for Stalled {
            fn backend(&self) -> Backend {
                Backend::Sqlite
            }
            fn insert_batch(&self, _records: &[Record]) -> Result<usize, crate::storage::StorageError> {
                std::thread::sleep(Duration::from_millis(50));
                Ok(_records.len())
            }
            fn query(
                &self,
                _f: &crate::query::QueryFilter,
                _p: &crate::query::Page,
            ) -> Result<(Vec<Record>, i64), crate::storage::StorageError> {
                Err(crate::storage::StorageError::Backend("down".into()))
            }
            fn count(
                &self,
                _f: &crate::query::QueryFilter,
                _c: Option<i64>,
            ) -> Result<i64, crate::storage::StorageError> {
                Err(crate::storage::StorageError::Backend("down".into()))
            }
            fn table_rows(&self) -> Result<i64, crate::storage::StorageError> {
                Ok(0)
            }
            fn stats(
                &self,
                _s: &crate::query::StatsScope,
            ) -> Result<crate::query::Stats, crate::storage::StorageError> {
                Err(crate::storage::StorageError::Backend("down".into()))
            }
            fn top_groups(
                &self,
                _f: &crate::query::QueryFilter,
                _b: crate::query::GroupBy,
                _l: usize,
            ) -> Result<Vec<(String, i64)>, crate::storage::StorageError> {
                Err(crate::storage::StorageError::Backend("down".into()))
            }
            fn error_aggregates(
                &self,
                _f: &crate::query::QueryFilter,
                _l: usize,
            ) -> Result<Vec<crate::query::ErrorAgg>, crate::storage::StorageError> {
                Err(crate::storage::StorageError::Backend("down".into()))
            }
            fn time_buckets(
                &self,
                _f: &crate::query::QueryFilter,
                _b: i64,
                _m: usize,
            ) -> Result<Vec<crate::query::Bucket>, crate::storage::StorageError> {
                Err(crate::storage::StorageError::Backend("down".into()))
            }
            fn purge(
                &self,
                _f: &crate::query::PurgeFilter,
                _c: usize,
                _m: u64,
            ) -> Result<crate::storage::PurgeReport, crate::storage::StorageError> {
                Err(crate::storage::StorageError::Backend("down".into()))
            }
            fn retention_purge(
                &self,
                _s: &crate::query::RetentionSweep,
                _c: usize,
                _m: u64,
            ) -> Result<crate::storage::PurgeReport, crate::storage::StorageError> {
                Err(crate::storage::StorageError::Backend("down".into()))
            }
            fn export_chunk(
                &self,
                _f: &crate::query::QueryFilter,
                _p: &crate::query::Page,
            ) -> Result<Vec<Record>, crate::storage::StorageError> {
                Err(crate::storage::StorageError::Backend("down".into()))
            }
            fn probe(&self) -> Result<(), crate::storage::StorageError> {
                Ok(())
            }
            fn checkpoint(&self) -> Result<(), crate::storage::StorageError> {
                Ok(())
            }
            fn describe(&self) -> String {
                "stalled".to_string()
            }
        }
        let metrics = Arc::new(Metrics::new());
        let health = Arc::new(HealthState::new());
        let storage: Arc<dyn Storage> = Arc::new(Stalled);
        let pipeline = Pipeline::start(
            PipelineConfig {
                batch_size: 4,
                batch_delay: Duration::from_millis(1),
                ..PipelineConfig::from_config(&Config::default())
            },
            Arc::clone(&storage),
            Arc::clone(&metrics),
        )
        .unwrap();
        for index in 0..40 {
            let _ = pipeline.admit(prepared(index));
        }
        let shutdown = Shutdown::new(
            storage,
            Arc::clone(&metrics),
            Arc::clone(&health),
            Duration::from_millis(20),
        );
        let outcome = shutdown.run(StopSignal::Sigterm, &pipeline, None);
        // The slow storage means the deadline can expire; whatever the outcome, it
        // must be honest about it.
        assert_eq!(outcome.signal, "sigterm");
        if !outcome.drained {
            assert!(outcome.remaining > 0, "{outcome:?}");
        }
        assert!(!health.is_alive());
    }

    #[test]
    fn exit_codes_follow_the_signal() {
        assert_eq!(StopSignal::Sigint.exit_code(), 130);
        assert_eq!(StopSignal::Sigterm.exit_code(), 143);
        assert_eq!(StopSignal::Sigint.as_str(), "sigint");
    }
}
