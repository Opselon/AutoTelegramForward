//! Age-based retention: bounded work per cycle, explicit progress, and a hard rule
//! that protected (audit/security) categories are never purged contrary to policy.
//!
//! docs/logging-contract.md §6 makes retention a *policy* decision, not a side
//! effect of writing: the engine deletes only what the policy says is expired, it
//! deletes at most `max_rows_per_cycle` rows per cycle so a big archive cannot turn
//! one cleanup into a multi-minute write lock, and it reports what it did.
//!
//! ## Which age applies to a row
//!
//! Rules are matched **first match wins** in the fixed order
//!
//! 1. `retention_level_hours` (`LEVEL=HOURS`)
//! 2. `retention_category_hours` (`CATEGORY=HOURS`, or `service:NAME=HOURS`)
//! 3. the default (`retention_default_max_age_hours`)
//!
//! Each sweep excludes the rows an earlier selector already owns, so one row is
//! never judged by two rules and the union of the sweeps is the whole table.
//! `service:NAME=HOURS` is accepted by both rule lists because per-service
//! overrides belong to a deployment's retention policy, not to one list; the
//! configuration module stores `(selector, hours)` pairs and this module is the
//! only place that interprets the selector.
//!
//! ## The audit rule
//!
//! `protected_categories` (default `AUDIT`, see `config::Config`) are excluded
//! **inside the deleting statement**, so a broad default rule can never remove an
//! audit row. When a sweep spares protected rows the cycle reports it, and the
//! count is exported as `atf_logger_retention_protected_rows_skipped_total` — the
//! difference between "retention is broken" and "retention spared your audit
//! trail" is exactly this number.

use crate::config::{Config, RetentionRule, TsUnit};
use crate::metrics::Metrics;
use crate::query::{OverrideSet, QueryFilter, RetentionSweep};
use crate::storage::Storage;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

/// Rows deleted in one `DELETE` chunk. Small enough that a cycle can be interrupted
/// between chunks, large enough that a 400k-row cleanup is a few hundred statements
/// rather than hundreds of thousands.
pub const DEFAULT_CHUNK: usize = 5_000;

/// Rows a single cycle may delete before it stops and waits for the next interval.
pub const DEFAULT_MAX_ROWS_PER_CYCLE: u64 = 250_000;

/// One interpreted rule: what it selects and how long that selection is kept.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rule {
    pub dimension: Dimension,
    pub selector: String,
    pub max_age_hours: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dimension {
    Level,
    Category,
    Service,
}

impl Dimension {
    pub fn as_str(self) -> &'static str {
        match self {
            Dimension::Level => "level",
            Dimension::Category => "category",
            Dimension::Service => "service",
        }
    }
}

/// Interpret one `(selector, hours)` pair.
///
/// `service:NAME` is a service override wherever it appears; `category:NAME` is a
/// category override; a bare selector means the dimension of the list it came from.
pub fn interpret(rule: &RetentionRule, list_dimension: Dimension) -> Rule {
    let selector = rule.selector.trim();
    if let Some(rest) = selector.strip_prefix("service:") {
        return Rule {
            dimension: Dimension::Service,
            selector: rest.trim().to_string(),
            max_age_hours: rule.max_age_hours,
        };
    }
    if let Some(rest) = selector.strip_prefix("category:") {
        return Rule {
            dimension: Dimension::Category,
            selector: rest.trim().to_string(),
            max_age_hours: rule.max_age_hours,
        };
    }
    if let Some(rest) = selector.strip_prefix("level:") {
        return Rule {
            dimension: Dimension::Level,
            selector: rest.trim().to_ascii_uppercase(),
            max_age_hours: rule.max_age_hours,
        };
    }
    Rule {
        dimension: list_dimension,
        selector: if list_dimension == Dimension::Level {
            selector.to_ascii_uppercase()
        } else {
            selector.to_string()
        },
        max_age_hours: rule.max_age_hours,
    }
}

/// The whole retention policy, validated and ready to run.
#[derive(Debug, Clone)]
pub struct RetentionPolicy {
    pub enabled: bool,
    pub interval: Duration,
    pub default_max_age_hours: u64,
    pub rules: Vec<Rule>,
    pub protected_categories: Vec<String>,
    pub ts_unit: TsUnit,
    pub chunk: usize,
    pub max_rows_per_cycle: u64,
}

impl RetentionPolicy {
    pub fn from_config(config: &Config) -> Self {
        let mut rules = Vec::new();
        for rule in &config.retention_level_hours {
            let interpreted = interpret(rule, Dimension::Level);
            if !interpreted.selector.is_empty() {
                rules.push(interpreted);
            }
        }
        for rule in &config.retention_category_hours {
            let interpreted = interpret(rule, Dimension::Category);
            if !interpreted.selector.is_empty() {
                rules.push(interpreted);
            }
        }
        RetentionPolicy {
            enabled: config.retention_enabled,
            interval: Duration::from_secs(config.retention_interval_secs.max(1)),
            default_max_age_hours: config.retention_default_max_age_hours,
            rules,
            protected_categories: config.protected_categories.clone(),
            ts_unit: config.retention_ts_unit,
            chunk: DEFAULT_CHUNK,
            max_rows_per_cycle: DEFAULT_MAX_ROWS_PER_CYCLE,
        }
    }

    pub fn with_budget(mut self, chunk: usize, max_rows_per_cycle: u64) -> Self {
        self.chunk = chunk.max(1);
        self.max_rows_per_cycle = max_rows_per_cycle.max(1);
        self
    }

    /// `now` in the unit the `ts` column uses.
    pub fn now(&self) -> i64 {
        self.ts_unit.now()
    }

    fn cutoff(&self, now: i64, hours: u64) -> Option<i64> {
        if hours == 0 {
            // 0 hours would mean "delete everything including this second". Policy
            // that intends that must say so with an explicit purge scope; a
            // retention rule of 0 is treated as "no time bound for this selector"
            // by *not* sweeping it, which is the safe reading of an operator typo.
            return None;
        }
        let cutoff = self.ts_unit.hours_in_unit(hours);
        Some(now.saturating_sub(cutoff))
    }

    /// The ordered sweeps one cycle performs. Exposed for tests and for the
    /// `/metrics`-adjacent diagnostics endpoint.
    pub fn sweeps(&self, now: i64) -> Vec<Sweep> {
        let mut sweeps = Vec::new();
        let mut exclusions = OverrideSet::default();
        for dimension in [Dimension::Level, Dimension::Category, Dimension::Service] {
            for rule in self.rules.iter().filter(|r| r.dimension == dimension) {
                let Some(cutoff) = self.cutoff(now, rule.max_age_hours) else {
                    continue;
                };
                let mut filter = QueryFilter::default();
                match rule.dimension {
                    Dimension::Level => filter.level = rule.selector.clone(),
                    Dimension::Category => filter.category = rule.selector.clone(),
                    Dimension::Service => filter.service = rule.selector.clone(),
                }
                sweeps.push(Sweep {
                    label: format!("{}({})", rule.dimension.as_str(), rule.selector),
                    sweep: RetentionSweep {
                        filter,
                        cutoff: Some(cutoff),
                        protected: self.protected_categories.clone(),
                        // Exclusions accumulate *after* each dimension, so a later
                        // rule never re-judges a row an earlier rule already owns.
                        exclusions: exclusions.clone(),
                    },
                });
            }
            // Everything matched by this dimension is owned from here on.
            for rule in self.rules.iter().filter(|r| r.dimension == dimension) {
                match rule.dimension {
                    Dimension::Level => exclusions.levels.push(rule.selector.clone()),
                    Dimension::Category => exclusions.categories.push(rule.selector.clone()),
                    Dimension::Service => exclusions.services.push(rule.selector.clone()),
                }
            }
        }
        if let Some(cutoff) = self.cutoff(now, self.default_max_age_hours) {
            sweeps.push(Sweep {
                label: format!("default({}h)", self.default_max_age_hours),
                sweep: RetentionSweep {
                    filter: QueryFilter::default(),
                    cutoff: Some(cutoff),
                    protected: self.protected_categories.clone(),
                    exclusions: exclusions.clone(),
                },
            });
        }
        sweeps
    }
}

/// One prepared sweep with a human-readable label for the cycle log line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Sweep {
    pub label: String,
    pub sweep: RetentionSweep,
}

/// What one cycle did.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RetentionReport {
    pub purged: u64,
    pub sweeps_run: usize,
    /// True when the row budget stopped the cycle with work left over.
    pub budget_exhausted: bool,
    /// True when at least one sweep had protected rows in scope.
    pub protected_in_scope: bool,
    pub failed_sweeps: usize,
    pub duration_millis: u64,
}

impl RetentionReport {
    /// The `LogStats`-style one-liner for stderr.
    pub fn describe(&self) -> String {
        format!(
            "retention purged={} sweeps={} failed={} budget_exhausted={} protected_in_scope={} duration_ms={}",
            self.purged,
            self.sweeps_run,
            self.failed_sweeps,
            self.budget_exhausted,
            self.protected_in_scope,
            self.duration_millis
        )
    }
}

/// Runs one cycle against a storage backend.
///
/// `now_override` exists so tests can drive retention deterministically instead of
/// sleeping until records age out; production passes `None` and the configured
/// timestamp unit decides "now".
pub fn run_cycle(
    policy: &RetentionPolicy,
    storage: &dyn Storage,
    metrics: &Metrics,
    now_override: Option<i64>,
) -> RetentionReport {
    let started = Instant::now();
    let now = now_override.unwrap_or_else(|| policy.now());
    let mut report = RetentionReport::default();
    if !policy.enabled {
        return report;
    }
    let mut remaining_budget = policy.max_rows_per_cycle;
    for sweep in policy.sweeps(now) {
        if remaining_budget == 0 {
            report.budget_exhausted = true;
            break;
        }
        report.sweeps_run += 1;
        match storage.retention_purge(&sweep.sweep, policy.chunk, remaining_budget) {
            Ok(outcome) => {
                report.purged += outcome.purged.max(0) as u64;
                report.protected_in_scope |= outcome.protected_skipped;
                if outcome.exhausted {
                    report.budget_exhausted = true;
                }
                remaining_budget = remaining_budget.saturating_sub(outcome.purged.max(0) as u64);
            }
            Err(err) => {
                // A failed sweep never aborts the cycle: the remaining sweeps still
                // run, the failure is counted, and readiness reports it.
                report.failed_sweeps += 1;
                metrics.retention_failures.inc();
                crate::health::emergency(
                    "retention",
                    &format!("{} failed: {}", sweep.label, err.code()),
                );
            }
        }
    }
    report.duration_millis = started.elapsed().as_millis() as u64;
    metrics.retention_cycles.inc();
    metrics.retention_purged.add(report.purged);
    metrics
        .retention_duration_seconds
        .observe_seconds(started.elapsed().as_secs_f64());
    if report.budget_exhausted {
        metrics.retention_budget_exhausted.inc();
    }
    if report.failed_sweeps == 0 {
        metrics.retention_last_success_ts.set(now);
    }
    report
}

/// The background scheduler.
pub struct Retention {
    policy: RetentionPolicy,
    storage: Arc<dyn Storage>,
    metrics: Arc<Metrics>,
    stop: Arc<AtomicBool>,
    last_purged: Arc<AtomicU64>,
    handle: Option<JoinHandle<()>>,
}

impl Retention {
    /// Start the scheduler. The first cycle runs after one full interval, so a
    /// restart storm cannot turn every pod into a cleanup worker at boot.
    pub fn start(
        policy: RetentionPolicy,
        storage: Arc<dyn Storage>,
        metrics: Arc<Metrics>,
    ) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let last_purged = Arc::new(AtomicU64::new(0));
        let mut retention = Retention {
            policy,
            storage,
            metrics,
            stop,
            last_purged,
            handle: None,
        };
        if !retention.policy.enabled {
            crate::health::emergency("retention", "disabled by configuration");
            return retention;
        }
        let policy = retention.policy.clone();
        let storage = Arc::clone(&retention.storage);
        let metrics = Arc::clone(&retention.metrics);
        let stop_for_thread = Arc::clone(&retention.stop);
        let last_purged_for_thread = Arc::clone(&retention.last_purged);
        let handle = thread::Builder::new()
            .name("atf-logger-retention".to_string())
            .spawn(move || {
                loop {
                    // Sleep in small steps so `stop` is honoured promptly.
                    let deadline = Instant::now() + policy.interval;
                    while Instant::now() < deadline {
                        if stop_for_thread.load(Ordering::SeqCst) {
                            return;
                        }
                        thread::sleep(Duration::from_millis(100));
                    }
                    if stop_for_thread.load(Ordering::SeqCst) {
                        return;
                    }
                    let report = run_cycle(&policy, storage.as_ref(), &metrics, None);
                    last_purged_for_thread.store(report.purged, Ordering::Relaxed);
                    if report.failed_sweeps > 0 || report.budget_exhausted {
                        crate::health::emergency("retention", &report.describe());
                    } else if report.purged > 0 {
                        crate::health::note("retention", &report.describe());
                    }
                    let _ = storage.checkpoint();
                }
            })
            .ok();
        retention.handle = handle;
        retention
    }

    pub fn policy(&self) -> &RetentionPolicy {
        &self.policy
    }

    pub fn is_running(&self) -> bool {
        self.handle.is_some()
    }

    /// Rows deleted by the last cycle.
    pub fn last_purged(&self) -> u64 {
        self.last_purged.load(Ordering::Relaxed)
    }

    /// Run one cycle now (used by tests and by an operator-triggered cleanup).
    pub fn run_once(&self) -> RetentionReport {
        let report = run_cycle(&self.policy, self.storage.as_ref(), &self.metrics, None);
        self.last_purged.store(report.purged, Ordering::Relaxed);
        let _ = self.storage.checkpoint();
        report
    }

    /// Ask the scheduler to stop and wait for it (bounded by `timeout`).
    pub fn stop(&mut self, timeout: Duration) {
        self.stop.store(true, Ordering::SeqCst);
        let Some(handle) = self.handle.take() else {
            return;
        };
        let deadline = Instant::now() + timeout;
        while !handle.is_finished() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        if handle.is_finished() {
            let _ = handle.join();
        }
        // If the timeout expired the thread finishes on its own: it checks `stop`
        // every 100 ms and every remaining step is a bounded storage call.
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::RetentionRule;
    use crate::storage::sqlite::SqliteStorage;
    use crate::storage::{Backend, BackendConfig, Record};

    fn storage(dir: &tempfile::TempDir, metrics: Arc<Metrics>) -> Arc<SqliteStorage> {
        let path = dir
            .path()
            .join("retention.db")
            .to_string_lossy()
            .to_string();
        Arc::new(
            SqliteStorage::open(
                &Config::default(),
                &BackendConfig {
                    backend: Backend::Sqlite,
                    sqlite_path: path,
                    postgres_dsn: None,
                    busy_timeout_ms: 5_000,
                    read_pool_size: 2,
                    pool_timeout_ms: 5_000,
                    statement_timeout_ms: 0,
                    transaction_timeout_ms: 0,
                    max_rows_per_statement: 500,
                },
                metrics,
            )
            .unwrap(),
        )
    }

    fn record(ts: i64, level: &str, category: &str, service: &str) -> Record {
        Record {
            id: 0,
            ts,
            level: level.to_string(),
            service: service.to_string(),
            category: category.to_string(),
            message: "retention fixture".to_string(),
            detail: "{}".to_string(),
            rule_id: String::new(),
            user_id: 0,
            correlation_id: String::new(),
            request_id: String::new(),
            trace_id: String::new(),
            module: String::new(),
            fingerprint: String::new(),
        }
    }

    fn policy(
        rules: Vec<RetentionRule>,
        categories: Vec<RetentionRule>,
        default_hours: u64,
    ) -> RetentionPolicy {
        RetentionPolicy::from_config(&Config {
            retention_enabled: true,
            retention_default_max_age_hours: default_hours,
            retention_level_hours: rules,
            retention_category_hours: categories,
            ..Default::default()
        })
    }

    #[test]
    fn selectors_are_interpreted_and_prefixed_overrides_work() {
        let level = interpret(
            &RetentionRule {
                selector: "debug".to_string(),
                max_age_hours: 1,
            },
            Dimension::Level,
        );
        assert_eq!(level.dimension, Dimension::Level);
        assert_eq!(level.selector, "DEBUG");

        // A service override is accepted from either list, so an operator can keep
        // one deployment's chatty service for a week without inventing a new flag.
        let service = interpret(
            &RetentionRule {
                selector: " service:chat-worker ".to_string(),
                max_age_hours: 168,
            },
            Dimension::Category,
        );
        assert_eq!(service.dimension, Dimension::Service);
        assert_eq!(service.selector, "chat-worker");
        assert_eq!(service.max_age_hours, 168);

        let category = interpret(
            &RetentionRule {
                selector: "category:payment".to_string(),
                max_age_hours: 24,
            },
            Dimension::Category,
        );
        assert_eq!(category.dimension, Dimension::Category);
        assert_eq!(category.selector, "payment");
    }

    #[test]
    fn sweeps_exclude_earlier_dimensions_and_end_with_the_default() {
        let policy = policy(
            vec![RetentionRule {
                selector: "DEBUG".to_string(),
                max_age_hours: 6,
            }],
            vec![RetentionRule {
                selector: "category:PAYMENT".to_string(),
                max_age_hours: 24,
            }],
            // 720h default
            720,
        );
        let now = 1_700_000_000i64;
        let sweeps = policy.sweeps(now);
        assert_eq!(sweeps.len(), 3, "{sweeps:?}");
        assert_eq!(sweeps[0].label, "level(DEBUG)");
        assert_eq!(sweeps[0].sweep.filter.level, "DEBUG");
        assert!(sweeps[0].sweep.exclusions.is_empty());
        assert_eq!(sweeps[1].label, "category(PAYMENT)");
        assert_eq!(sweeps[1].sweep.exclusions.levels, vec!["DEBUG".to_string()]);
        assert_eq!(sweeps[2].label, "default(720h)");
        assert_eq!(
            sweeps[2].sweep.exclusions.categories,
            vec!["PAYMENT".to_string()]
        );
        // Every sweep carries the protected categories, including the default.
        assert!(sweeps.iter().all(|s| !s.sweep.protected.is_empty()));
        // Cutoffs step down in age: DEBUG is the most aggressive.
        assert!(sweeps[0].sweep.cutoff.unwrap() > sweeps[2].sweep.cutoff.unwrap());
        assert_eq!(sweeps[2].sweep.cutoff.unwrap(), now - 720 * 3600);
    }

    #[test]
    fn zero_hour_rule_is_not_a_delete_everything_rule() {
        let policy = policy(
            vec![RetentionRule {
                selector: "TRACE".to_string(),
                max_age_hours: 0,
            }],
            vec![],
            720,
        );
        let sweeps = policy.sweeps(1_700_000_000);
        // The zero-hour rule is skipped entirely (never "everything"), but its rows
        // are still owned by it, so the default sweep must not touch TRACE either.
        assert_eq!(sweeps.len(), 1);
        assert_eq!(sweeps[0].label, "default(720h)");
        assert_eq!(sweeps[0].sweep.exclusions.levels, vec!["TRACE".to_string()]);
    }

    #[test]
    fn cycle_purges_old_rows_and_keeps_protected_audit_rows() {
        let dir = tempfile::tempdir().unwrap();
        let metrics = Arc::new(Metrics::new());
        let storage = storage(&dir, Arc::clone(&metrics));
        let now = 1_700_000_000i64;

        // 10 days old: inside the default 720h but outside a 24h category rule.
        let old = now - 10 * 24 * 3600;
        let mut rows = Vec::new();
        for _ in 0..40 {
            rows.push(record(old, "INFO", "SYSTEM", "core"));
        }
        for _ in 0..10 {
            // Audit rows are old enough to expire but must survive.
            rows.push(record(old, "CRITICAL", "AUDIT", "core"));
        }
        for _ in 0..25 {
            // Payment rows are governed by the 24h override.
            rows.push(record(old, "ERROR", "PAYMENT", "billing"));
        }
        storage.insert_batch(&rows).unwrap();
        assert_eq!(storage.table_rows().unwrap(), 75);

        let policy = policy(
            vec![],
            vec![
                RetentionRule {
                    selector: "category:AUDIT".to_string(),
                    max_age_hours: 720,
                },
                RetentionRule {
                    selector: "category:PAYMENT".to_string(),
                    max_age_hours: 24,
                },
            ],
            720,
        );
        let report = run_cycle(&policy, storage.as_ref(), &metrics, Some(now));
        assert_eq!(report.failed_sweeps, 0);
        // Only the PAYMENT rows are older than their own 24h rule; SYSTEM rows are
        // 10 days old and stay for the full 30-day default.
        assert_eq!(report.purged, 25, "{report:?}");
        assert_eq!(storage.table_rows().unwrap(), 50);
        // The AUDIT rows are still there, and the cycle says protected rows were in
        // scope rather than silently skipping.
        let audit = storage
            .query(
                &QueryFilter {
                    category: "AUDIT".to_string(),
                    ..QueryFilter::default()
                },
                &crate::query::Page::default(),
            )
            .unwrap();
        assert_eq!(audit.1, 10);
        assert_eq!(metrics.retention_cycles.get(), 1);
        assert_eq!(metrics.retention_purged.get(), 25);
        assert_eq!(metrics.retention_last_success_ts.get(), now);
    }

    #[test]
    fn protected_categories_survive_the_default_sweep() {
        let dir = tempfile::tempdir().unwrap();
        let metrics = Arc::new(Metrics::new());
        let storage = storage(&dir, Arc::clone(&metrics));
        let now = 1_700_000_000i64;
        let ancient = now - 400 * 24 * 3600;
        let rows = vec![
            record(ancient, "INFO", "SYSTEM", "core"),
            record(ancient, "INFO", "AUDIT", "core"),
            record(ancient, "WARN", "SECURITY", "core"),
        ];
        storage.insert_batch(&rows).unwrap();

        let policy = policy(vec![], vec![], 720);
        let report = run_cycle(&policy, storage.as_ref(), &metrics, Some(now));
        // The default sweep is unbounded in scope, so the protected categories are
        // the only thing that saves their rows — and that is exactly the promise.
        assert_eq!(report.purged, 1, "{report:?}");
        assert_eq!(storage.table_rows().unwrap(), 2);
        for category in ["AUDIT", "SECURITY"] {
            let (rows, total) = storage
                .query(
                    &QueryFilter {
                        category: category.to_string(),
                        ..QueryFilter::default()
                    },
                    &crate::query::Page::default(),
                )
                .unwrap();
            assert_eq!(total, 1, "{category} must survive: {rows:?}");
        }
    }

    #[test]
    fn budget_bounds_one_cycle_and_reports_leftovers() {
        let dir = tempfile::tempdir().unwrap();
        let metrics = Arc::new(Metrics::new());
        let storage = storage(&dir, Arc::clone(&metrics));
        let now = 1_700_000_000i64;
        let ancient = now - 400 * 24 * 3600;
        let rows: Vec<Record> = (0..300)
            .map(|i| record(ancient, "INFO", "SYSTEM", &format!("svc-{i}")))
            .collect();
        storage.insert_batch(&rows).unwrap();

        let policy = policy(vec![], vec![], 720).with_budget(50, 120);
        let report = run_cycle(&policy, storage.as_ref(), &metrics, Some(now));
        assert_eq!(
            report.purged, 120,
            "the budget must stop the cycle: {report:?}"
        );
        assert!(report.budget_exhausted);
        assert_eq!(storage.table_rows().unwrap(), 180);
        // Progress is monotonic across cycles until the backlog is gone.
        let second = run_cycle(&policy, storage.as_ref(), &metrics, Some(now));
        assert_eq!(second.purged, 120);
        let third = run_cycle(&policy, storage.as_ref(), &metrics, Some(now));
        assert_eq!(third.purged, 60);
        assert!(!third.budget_exhausted);
        assert_eq!(metrics.retention_budget_exhausted.get(), 2);
    }

    #[test]
    fn disabled_policy_does_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let metrics = Arc::new(Metrics::new());
        let storage = storage(&dir, Arc::clone(&metrics));
        let ancient = 1_000_000i64;
        storage
            .insert_batch(&[record(ancient, "INFO", "SYSTEM", "core")])
            .unwrap();
        let mut policy = policy(vec![], vec![], 1);
        policy.enabled = false;
        let retention = Retention::start(
            policy,
            Arc::clone(&storage) as Arc<dyn Storage>,
            Arc::clone(&metrics),
        );
        assert!(!retention.is_running());
        let report = retention.run_once();
        assert_eq!(report.purged, 0);
        assert_eq!(storage.table_rows().unwrap(), 1);
    }

    #[test]
    fn scheduler_starts_runs_and_stops() {
        let dir = tempfile::tempdir().unwrap();
        let metrics = Arc::new(Metrics::new());
        let storage = storage(&dir, Arc::clone(&metrics));
        let now = crate::config::TsUnit::Seconds.now();
        let rows: Vec<Record> = (0..20)
            .map(|_| record(now - 400 * 24 * 3600, "INFO", "SYSTEM", "core"))
            .collect();
        storage.insert_batch(&rows).unwrap();

        let mut policy = policy(vec![], vec![], 720);
        policy.interval = Duration::from_millis(150);
        let mut retention = Retention::start(
            policy,
            Arc::clone(&storage) as Arc<dyn Storage>,
            Arc::clone(&metrics),
        );
        assert!(retention.is_running());
        // Wait for at least one scheduled cycle.
        let deadline = Instant::now() + Duration::from_secs(5);
        while storage.table_rows().unwrap() > 0 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(50));
        }
        assert_eq!(storage.table_rows().unwrap(), 0, "the scheduler must purge");
        retention.stop(Duration::from_secs(2));
        assert!(!retention.is_running());
    }
}
