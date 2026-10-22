//! Storage abstraction: one trait, two first-class backends.
//!
//! docs/logging-contract.md §4: *"Both SQLite and PostgreSQL are supported
//! first-class; the choice is explicit validated configuration, and Logger never
//! silently falls back to a different backend after a startup failure — it fails
//! loudly instead, because a Logger quietly writing to the wrong database is worse
//! than a Logger that will not start."*
//!
//! That rule is enforced in three places:
//!
//! * [`BackendConfig::from_config`] decides the backend from explicit input only
//!   (`ATF_LOGGER_BACKEND`, a `postgres://` DSN, or `ATF_LOGGER_PG_DSN`), and
//!   refuses a PostgreSQL selection that has no DSN instead of guessing.
//! * [`open`] returns an error when the selected backend cannot be opened; there is
//!   no `unwrap_or_else(|| sqlite())` anywhere.
//! * `main` exits non-zero, with an emergency stderr line naming the failed
//!   backend, rather than continuing with a different one.
//!
//! The trait is deliberately *synchronous*. Both drivers are synchronous
//! (`rusqlite`; the `postgres` crate's blocking `Client` wrapper), the engine wants
//! a single writer path, and every call site already runs inside
//! `tokio::task::spawn_blocking`. Going async here would add a runtime dependency
//! to the storage layer without buying throughput the batched writer does not
//! already have.

pub mod postgres;
pub mod sqlite;

use crate::config::Config;
use crate::metrics::Metrics;
use crate::query::{
    Bucket, ErrorAgg, GroupBy, Page, PurgeFilter, QueryFilter, RetentionSweep, Stats, StatsScope,
};
use std::sync::Arc;
use std::time::Duration;

/// One stored log record. Field names and order match the Python `logs` table.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Record {
    /// `INTEGER PRIMARY KEY AUTOINCREMENT`; 0 for a record that is not stored yet.
    pub id: i64,
    pub ts: i64,
    pub level: String,
    pub service: String,
    pub category: String,
    pub message: String,
    pub detail: String,
    pub rule_id: String,
    pub user_id: i64,
    pub correlation_id: String,
    /// Structured context columns. They ride the v19 additive migration, so an
    /// old archive stores `''` and every read is unchanged. `fingerprint` is set
    /// by the engine at ingest (never accepted from the wire key of the same
    /// name on a read, because a client cannot forge a consistent identity).
    pub request_id: String,
    pub trace_id: String,
    pub module: String,
    pub fingerprint: String,
}

/// Which storage engine is in use.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Backend {
    Sqlite,
    Postgres,
}

impl Backend {
    pub fn as_str(self) -> &'static str {
        match self {
            Backend::Sqlite => "sqlite",
            Backend::Postgres => "postgres",
        }
    }
}

/// Storage failure. `is_retryable` is what separates "try again in a moment" from
/// "this will never work" — conflating the two is how a batch gets silently lost.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StorageError {
    /// A lock/busy/timeout condition: safe to retry.
    Busy(String),
    /// A transient backend error (serialization failure, connection reset).
    Transient(String),
    /// A real failure: the statement or transaction is broken.
    Backend(String),
    /// The store cannot be reached at all (connect refused, pool exhausted at
    /// startup, archive not openable). Distinct from `Backend`, which means the
    /// store answered but the statement failed: the producer must be able to tell
    /// "try again later" from "your write is malformed".
    Unavailable(String),
    /// Misconfiguration or startup failure.
    Config(String),
    /// The operation is not supported by this backend.
    Unsupported(&'static str),
}

impl StorageError {
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            StorageError::Busy(_) | StorageError::Transient(_) | StorageError::Unavailable(_)
        )
    }

    /// Reclassify an open-time failure as "the store is not reachable". `Config`
    /// and `Unsupported` keep their own codes: those are the operator's mistakes,
    /// not a transient outage, and telling them apart is the whole point.
    pub fn as_unavailable(self) -> StorageError {
        match self {
            StorageError::Config(_)
            | StorageError::Unsupported(_)
            | StorageError::Unavailable(_) => self,
            other => StorageError::Unavailable(other.message()),
        }
    }

    /// Stable code for the gRPC ack path (never a raw backend message).
    pub fn code(&self) -> &'static str {
        match self {
            StorageError::Busy(_) => "STORAGE_BUSY",
            StorageError::Transient(_) => "STORAGE_TRANSIENT",
            StorageError::Backend(_) => "STORAGE_ERROR",
            StorageError::Unavailable(_) => "STORAGE_UNAVAILABLE",
            StorageError::Config(_) => "STORAGE_CONFIG",
            StorageError::Unsupported(_) => "STORAGE_UNSUPPORTED",
        }
    }

    pub fn message(&self) -> String {
        match self {
            StorageError::Busy(m)
            | StorageError::Transient(m)
            | StorageError::Unavailable(m)
            | StorageError::Backend(m)
            | StorageError::Config(m) => m.clone(),
            StorageError::Unsupported(m) => format!("unsupported by this backend: {m}"),
        }
    }
}

impl std::fmt::Display for StorageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{}] {}", self.code(), self.message())
    }
}

impl std::error::Error for StorageError {}

/// Result of a purge (either the RPC or one retention cycle).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PurgeReport {
    pub purged: i64,
    /// Rows left in the table (whole table, matching the Python engine).
    pub remaining: i64,
    /// True when the row budget stopped the purge before every match was deleted.
    pub exhausted: bool,
    /// True when the filter would have matched protected categories and they were
    /// spared. Never silent: the transport reports this.
    pub protected_skipped: bool,
}

/// Explicit backend configuration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackendConfig {
    pub backend: Backend,
    /// SQLite file path (unused for PostgreSQL).
    pub sqlite_path: String,
    /// PostgreSQL DSN (unused for SQLite).
    pub postgres_dsn: Option<String>,
    pub busy_timeout_ms: u64,
    pub read_pool_size: usize,
    /// How long a caller waits for a pooled connection before it is a busy error.
    pub pool_timeout_ms: u64,
    /// PostgreSQL `statement_timeout`.
    pub statement_timeout_ms: u64,
    /// PostgreSQL `idle_in_transaction_session_timeout`.
    pub transaction_timeout_ms: u64,
    /// Rows per write statement (a batch larger than this is split inside one
    /// transaction).
    pub max_rows_per_statement: usize,
}

impl BackendConfig {
    /// Decide the backend from explicit configuration only.
    ///
    /// Order of precedence:
    /// 1. `ATF_LOGGER_BACKEND=sqlite|postgres|postgresql`
    /// 2. a `db_path` that is a `postgres://` / `postgresql://` DSN
    /// 3. `ATF_LOGGER_PG_DSN` / `ATF_LOGGER_PG_URL` being set (implies PostgreSQL)
    /// 4. otherwise SQLite at `db_path`
    pub fn from_config(
        cfg: &Config,
        env: &dyn Fn(&str) -> Option<String>,
    ) -> Result<Self, StorageError> {
        let dsn_env = env("ATF_LOGGER_PG_DSN")
            .or_else(|| env("ATF_LOGGER_PG_URL"))
            .filter(|v| !v.trim().is_empty());
        let db_path = cfg.db_path.trim().to_string();
        let path_is_dsn = is_postgres_dsn(&db_path);
        let explicit = env("ATF_LOGGER_BACKEND")
            .map(|v| v.trim().to_ascii_lowercase())
            .filter(|v| !v.is_empty());

        // A scheme we do not speak is refused, never guessed: without this,
        // "mysql://host/db" would be accepted and used as a literal SQLite
        // filename, and the operator would find out by finding an empty archive.
        if !path_is_dsn && db_path.contains("://") {
            return Err(StorageError::Config(
                "unsupported database DSN scheme: expected a postgres:// or                  postgresql:// DSN, or a plain SQLite file path"
                    .to_string(),
            ));
        }

        let backend = match explicit.as_deref() {
            Some("sqlite") => Backend::Sqlite,
            Some("postgres") | Some("postgresql") | Some("pg") => Backend::Postgres,
            Some(other) => {
                return Err(StorageError::Config(format!(
                    "ATF_LOGGER_BACKEND must be sqlite or postgres, got {other}"
                )))
            }
            None => {
                if path_is_dsn || dsn_env.is_some() {
                    Backend::Postgres
                } else {
                    Backend::Sqlite
                }
            }
        };

        match backend {
            Backend::Postgres => {
                let dsn = if path_is_dsn { Some(db_path) } else { dsn_env };
                let dsn = dsn.ok_or_else(|| {
                    StorageError::Config(
                        "PostgreSQL was selected but no DSN was supplied: set ATF_LOGGER_PG_DSN \
                         or pass --db postgres://user:pass@host/dbname"
                            .to_string(),
                    )
                })?;
                if !is_postgres_dsn(&dsn) {
                    return Err(StorageError::Config(
                        "PostgreSQL DSN must start with postgres:// or postgresql://".to_string(),
                    ));
                }
                Ok(BackendConfig {
                    backend,
                    sqlite_path: String::new(),
                    postgres_dsn: Some(dsn),
                    busy_timeout_ms: cfg.busy_timeout_ms,
                    read_pool_size: cfg.read_pool_size,
                    pool_timeout_ms: cfg.busy_timeout_ms,
                    statement_timeout_ms: cfg.purge_timeout_ms.min(60_000),
                    transaction_timeout_ms: cfg.ack_timeout_ms.max(1_000),
                    max_rows_per_statement: 1000,
                })
            }
            Backend::Sqlite => {
                if db_path.is_empty() {
                    return Err(StorageError::Config(
                        "SQLite was selected but ATF_LOGGER_DB_PATH is empty".to_string(),
                    ));
                }
                Ok(BackendConfig {
                    backend,
                    sqlite_path: db_path,
                    postgres_dsn: None,
                    busy_timeout_ms: cfg.busy_timeout_ms,
                    read_pool_size: cfg.read_pool_size,
                    pool_timeout_ms: cfg.busy_timeout_ms,
                    statement_timeout_ms: 0,
                    transaction_timeout_ms: 0,
                    max_rows_per_statement: 500,
                })
            }
        }
    }

    pub fn describe(&self) -> String {
        match self.backend {
            Backend::Sqlite => format!(
                "backend=sqlite path={} read_pool={} busy_timeout_ms={} rows_per_statement={}",
                self.sqlite_path, self.read_pool_size, self.busy_timeout_ms, self.max_rows_per_statement
            ),
            Backend::Postgres => format!(
                "backend=postgres pool={} pool_timeout_ms={} statement_timeout_ms={} rows_per_statement={}",
                self.read_pool_size, self.pool_timeout_ms, self.statement_timeout_ms, self.max_rows_per_statement
            ),
        }
    }

    /// Redacted description that never prints credentials.
    pub fn describe_safe(&self) -> String {
        match self.backend {
            Backend::Sqlite => self.describe(),
            Backend::Postgres => {
                let dsn = self.postgres_dsn.as_deref().unwrap_or("");
                let host = dsn
                    .split('@')
                    .next_back()
                    .unwrap_or("(unknown)")
                    .split('/')
                    .next()
                    .unwrap_or("(unknown)");
                format!(
                    "backend=postgres host={host} pool={} pool_timeout_ms={} statement_timeout_ms={}",
                    self.read_pool_size, self.pool_timeout_ms, self.statement_timeout_ms
                )
            }
        }
    }
}

/// True for a PostgreSQL connection string.
pub fn is_postgres_dsn(value: &str) -> bool {
    let trimmed = value.trim().to_ascii_lowercase();
    trimmed.starts_with("postgres://") || trimmed.starts_with("postgresql://")
}

/// The storage contract. Every method is expected to be called from a blocking
/// context (see the module docs).
pub trait Storage: Send + Sync {
    fn backend(&self) -> Backend;

    /// Insert a batch in ONE transaction. Returns the number of rows written.
    /// `success` on the wire means this returned `Ok`.
    fn insert_batch(&self, records: &[Record]) -> Result<usize, StorageError>;

    /// One page of records plus the total match count.
    fn query(&self, filter: &QueryFilter, page: &Page) -> Result<(Vec<Record>, i64), StorageError>;

    /// Match count. `cap` bounds the work (`Some(10_000)` → the statement stops
    /// after at most that many rows, "10k+" is enough for a UI); `None` is an exact
    /// unbounded count and belongs only on paths that genuinely need it.
    fn count(&self, filter: &QueryFilter, cap: Option<i64>) -> Result<i64, StorageError>;

    /// Total rows in the archive.
    fn table_rows(&self) -> Result<i64, StorageError>;

    /// The `LogStats` aggregate.
    fn stats(&self, scope: &StatsScope) -> Result<Stats, StorageError>;

    /// Top recurring groups for one dimension.
    fn top_groups(
        &self,
        filter: &QueryFilter,
        by: GroupBy,
        limit: usize,
    ) -> Result<Vec<(String, i64)>, StorageError>;

    /// Recurring failures, newest first.
    fn error_aggregates(
        &self,
        filter: &QueryFilter,
        limit: usize,
    ) -> Result<Vec<ErrorAgg>, StorageError>;

    /// Time-bucketed counts.
    fn time_buckets(
        &self,
        filter: &QueryFilter,
        bucket_secs: i64,
        max_buckets: usize,
    ) -> Result<Vec<Bucket>, StorageError>;

    /// Delete matching rows, in bounded chunks, up to `max_rows` per call.
    fn purge(
        &self,
        filter: &PurgeFilter,
        chunk: usize,
        max_rows: u64,
    ) -> Result<PurgeReport, StorageError>;

    /// One bounded age-based retention sweep.
    ///
    /// Same contract as `purge` but expressed as a [`query::RetentionSweep`], which
    /// carries the override exclusions an earlier sweep already owns. The
    /// protected-category guard is still applied inside the deleting statement, so
    /// this can never remove a protected row.
    fn retention_purge(
        &self,
        sweep: &RetentionSweep,
        chunk: usize,
        max_rows: u64,
    ) -> Result<PurgeReport, StorageError>;

    /// One chunk of a bounded-memory export (keyset-paged).
    fn export_chunk(&self, filter: &QueryFilter, page: &Page) -> Result<Vec<Record>, StorageError>;

    /// Cheap readiness probe: the connection works and the schema is usable.
    fn probe(&self) -> Result<(), StorageError>;

    /// Flush/compact after a purge (SQLite: WAL checkpoint).
    fn checkpoint(&self) -> Result<(), StorageError>;

    /// Human-readable identity for diagnostics.
    fn describe(&self) -> String;
}

/// Open the explicitly configured backend, or fail loudly.
///
/// There is deliberately no fallback branch: a Logger that cannot reach its
/// database must not start writing somewhere else.
pub fn open(
    cfg: &Config,
    backend: &BackendConfig,
    metrics: Arc<Metrics>,
) -> Result<Arc<dyn Storage>, StorageError> {
    // Note the absence of `?` inside the arms: the error has to be captured and
    // reclassified, not propagated raw. A failure to open the store at all is an
    // availability problem -- there is no storage behind this process yet -- which
    // is exactly the condition main.rs reports as `storage_unavailable` on the
    // readiness surface. The code a producer sees must agree with that.
    let opened = match backend.backend {
        Backend::Sqlite => sqlite::SqliteStorage::open(cfg, backend, metrics)
            .map(|storage| Arc::new(storage) as Arc<dyn Storage>),
        Backend::Postgres => postgres::PgStorage::open(cfg, backend, metrics)
            .map(|storage| Arc::new(storage) as Arc<dyn Storage>),
    };
    opened.map_err(StorageError::as_unavailable)
}

/// Insert with bounded retries on retryable failures.
///
/// Called from the writer thread (blocking), so the backoff uses `thread::sleep`.
/// Every retry is counted; a batch that still fails is reported to its callers so
/// their acks say `false` — never a silent drop.
pub fn insert_batch_with_retry(
    storage: &dyn Storage,
    records: &[Record],
    max_retries: u32,
    retry_base_ms: u64,
    metrics: &Metrics,
) -> Result<usize, StorageError> {
    let mut attempt = 0u32;
    loop {
        match storage.insert_batch(records) {
            Ok(written) => return Ok(written),
            Err(err) => {
                if !err.is_retryable() || attempt >= max_retries {
                    return Err(err);
                }
                attempt += 1;
                metrics.events_retried.inc();
                metrics.storage_transaction_failures.inc();
                // Exponential with a cap: a long outage must not become a hammer.
                let backoff = retry_base_ms
                    .saturating_mul(1u64 << attempt.min(6))
                    .clamp(1, 5_000);
                std::thread::sleep(Duration::from_millis(backoff));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;

    fn no_env(_: &str) -> Option<String> {
        None
    }

    fn env_with<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |key: &str| {
            pairs
                .iter()
                .find(|(k, _)| *k == key)
                .map(|(_, v)| (*v).to_string())
        }
    }

    #[test]
    fn backend_defaults_to_sqlite() {
        let cfg = Config::default();
        let backend = BackendConfig::from_config(&cfg, &no_env).unwrap();
        assert_eq!(backend.backend, Backend::Sqlite);
        assert_eq!(backend.sqlite_path, "data/atf_logs.db");
        assert_eq!(backend.max_rows_per_statement, 500);
    }

    #[test]
    fn postgres_can_be_selected_by_dsn_in_db_path() {
        let cfg = Config {
            db_path: "postgres://user:pw@db:5432/atf".to_string(),
            ..Default::default()
        };
        let backend = BackendConfig::from_config(&cfg, &no_env).unwrap();
        assert_eq!(backend.backend, Backend::Postgres);
        assert!(backend.postgres_dsn.is_some());
        // Credentials never appear in the diagnostic string.
        assert!(!backend.describe_safe().contains("pw"));
    }

    #[test]
    fn postgres_can_be_selected_explicitly_and_requires_a_dsn() {
        let env = env_with(&[("ATF_LOGGER_BACKEND", "postgres")]);
        let cfg = Config::default();
        let err = BackendConfig::from_config(&cfg, &env).unwrap_err();
        assert_eq!(err.code(), "STORAGE_CONFIG");
        let env = env_with(&[
            ("ATF_LOGGER_BACKEND", "postgres"),
            ("ATF_LOGGER_PG_DSN", "postgresql://u@h:5432/atf"),
        ]);
        let backend = BackendConfig::from_config(&cfg, &env).unwrap();
        assert_eq!(backend.backend, Backend::Postgres);
        assert_eq!(backend.max_rows_per_statement, 1000);
    }

    #[test]
    fn unknown_backend_is_refused_not_guessed() {
        let env = env_with(&[("ATF_LOGGER_BACKEND", "mysql")]);
        let err = BackendConfig::from_config(&Config::default(), &env).unwrap_err();
        assert_eq!(err.code(), "STORAGE_CONFIG");
    }

    #[test]
    fn dsn_detection_is_strict() {
        assert!(is_postgres_dsn("postgres://a"));
        assert!(is_postgres_dsn("POSTGRESQL://a"));
        assert!(!is_postgres_dsn("/var/lib/atf/logs.db"));
        assert!(!is_postgres_dsn("data/atf_logs.db"));
    }

    #[test]
    fn error_classification_drives_retry() {
        assert!(StorageError::Busy("locked".into()).is_retryable());
        assert!(StorageError::Transient("reset".into()).is_retryable());
        assert!(!StorageError::Backend("bad sql".into()).is_retryable());
        assert_eq!(StorageError::Busy("x".into()).code(), "STORAGE_BUSY");
        assert!(StorageError::Unsupported("x")
            .to_string()
            .contains("unsupported"));
    }
}
