//! SQLite storage: the archive owner and the compatibility contract with the
//! Python engine's `logs` table.
//!
//! Compatibility is not a nice-to-have here. Existing deployments have an archive
//! created by `logger/main.py`; the Rust engine must open it, read it, keep writing
//! to it, and leave every existing row readable. So this module:
//!
//! * creates the **same table** (`id INTEGER PRIMARY KEY AUTOINCREMENT, ts, level,
//!   service, category, message, detail`, plus the v18 `rule_id`, `user_id`,
//!   `correlation_id` columns) with the **same migration shape** — `ALTER TABLE
//!   ADD COLUMN` with `NOT NULL DEFAULT`, which is instant on a multi-gigabyte
//!   archive, instead of a table rebuild;
//! * creates the **same seven indexes** with the **same names**, so a database
//!   created by either engine is optimised for the other's queries and no index is
//!   duplicated under a second name;
//! * keeps `journal_mode=WAL` + `synchronous=FULL`, because that is what makes the
//!   Python engine's `success=true` mean "committed and fsynced" (docs/logging-
//!   contract.md §4). Weakening it would silently invalidate every producer's
//!   assumption that an ack survives power loss.
//!
//! What is deliberately *different*, and why:
//!
//! * **One writer connection + a read pool.** The Python engine funnelled every
//!   operation, reads included, through one `threading.Lock`. Reads here are routed
//!   to `read_pool_size` connections, so a slow dashboard query cannot delay a
//!   commit and a commit cannot delay a dashboard query. Writes still go through
//!   exactly one connection: batching, not parallel writers, is what raises SQLite
//!   throughput (contract §4).
//! * **Batched inserts in one transaction.** One `INSERT` per record with a commit
//!   per record is the measured ~1,150 events/s ceiling; N rows per statement and
//!   one commit per batch is the entire point of this engine.
//! * **No `SELECT *`.** The column list is explicit (`query::COLUMNS`), so a column
//!   added by a future migration cannot silently widen every response.
//! * **`wal_checkpoint(PASSIVE)` only after a purge**, never on the ingest path —
//!   identical to the Python engine's placement, but now also explicit.
//!
//! Prepared statements: read statements have fixed text and are cached
//! (`prepare_cached`). Write statements vary with the chunk size, so they are
//! prepared per chunk; caching a 500-row `INSERT` buys nothing next to executing it.

use super::{Backend, BackendConfig, PurgeReport, Record, Storage, StorageError};
use crate::config::Config;
use crate::metrics::Metrics;
use crate::query::{
    self, Bucket, ErrorAgg, GroupBy, Page, Param, PurgeFilter, QueryFilter, RetentionSweep,
    SqlDialect, Stats, StatsScope, MAX_GROUPS, MAX_RULE_GROUPS,
};
use rusqlite::{Connection, OpenFlags};
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// SQLite's `SQLITE_BUSY`.
const SQLITE_BUSY: i32 = 5;
/// SQLite's `SQLITE_LOCKED`.
const SQLITE_LOCKED: i32 = 6;
/// SQLite's `SQLITE_IOERR`.
const SQLITE_IOERR: i32 = 10;
/// SQLite's `SQLITE_BUSY_SNAPSHOT` (extended code inside SQLITE_BUSY).
const SQLITE_BUSY_SNAPSHOT: i32 = 517;
/// SQLite's `SQLITE_BUSY_TIMEOUT`.
const SQLITE_BUSY_TIMEOUT: i32 = 773;
/// SQLite's `SQLITE_FULL`: the database (or its journal/WAL) hit the disk
/// limit. An environmental condition, not a statement bug, so it must be
/// retryable and distinguishable from a broken statement.
const SQLITE_FULL: i32 = 13;
/// `SQLITE_IOERR_WRITE` / `SQLITE_IOERR_FSYNC`: the write/fsync itself failed
/// (typically "no space left on device" surfaces here rather than as FULL).
const SQLITE_IOERR_WRITE: i32 = 778;
const SQLITE_IOERR_FSYNC: i32 = 582;

/// The Python engine's schema, verbatim in behaviour.
pub const CREATE_TABLE: &str = "\
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    service TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
)";

/// The v18 attribution columns plus the v19 structured-context columns, added
/// by migration rather than a rebuild.
///
/// Every statement is `ADD COLUMN ... NOT NULL DEFAULT`, which SQLite applies
/// without rewriting the table, so opening an archive written by an older engine
/// is an in-place, non-destructive migration (the case `tests/grpc_end_to_end.rs`
/// pins with a Python-created database).
pub const MIGRATION_COLUMNS: [(&str, &str); 7] = [
    (
        "rule_id",
        "ALTER TABLE logs ADD COLUMN rule_id TEXT NOT NULL DEFAULT ''",
    ),
    (
        "user_id",
        "ALTER TABLE logs ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "correlation_id",
        "ALTER TABLE logs ADD COLUMN correlation_id TEXT NOT NULL DEFAULT ''",
    ),
    (
        "request_id",
        "ALTER TABLE logs ADD COLUMN request_id TEXT NOT NULL DEFAULT ''",
    ),
    (
        "trace_id",
        "ALTER TABLE logs ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''",
    ),
    (
        "module",
        "ALTER TABLE logs ADD COLUMN module TEXT NOT NULL DEFAULT ''",
    ),
    (
        "fingerprint",
        "ALTER TABLE logs ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''",
    ),
];

/// The Python engine's index set (same names, same columns) plus the two
/// indexes the structured-context queries need.
///
/// Only `trace_id` and `fingerprint` get one: both are high-selectivity
/// equality/lookup keys on a hot path (`QueryLogs(trace_id=…)`,
/// `QueryLogs(fingerprint=…)`, and the error-group aggregate). `request_id` is
/// deliberately left unindexed — it is almost always accompanied by a
/// `trace_id`/`correlation_id` predicate in practice, and a third index would
/// cost every insert to speed up a lookup that already has a bound.
pub const INDEXES: [(&str, &str); 9] = [
    (
        "idx_logs_ts",
        "CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)",
    ),
    (
        "idx_logs_level",
        "CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)",
    ),
    (
        "idx_logs_category",
        "CREATE INDEX IF NOT EXISTS idx_logs_category ON logs(category)",
    ),
    (
        "idx_logs_service",
        "CREATE INDEX IF NOT EXISTS idx_logs_service ON logs(service)",
    ),
    (
        "idx_logs_rule",
        "CREATE INDEX IF NOT EXISTS idx_logs_rule ON logs(rule_id)",
    ),
    (
        "idx_logs_user",
        "CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id)",
    ),
    (
        "idx_logs_corr",
        "CREATE INDEX IF NOT EXISTS idx_logs_corr ON logs(correlation_id)",
    ),
    (
        "idx_logs_trace",
        "CREATE INDEX IF NOT EXISTS idx_logs_trace ON logs(trace_id)",
    ),
    (
        "idx_logs_fingerprint",
        "CREATE INDEX IF NOT EXISTS idx_logs_fingerprint ON logs(fingerprint)",
    ),
];

/// The single writer plus the read pool.
pub struct SqliteStorage {
    path: String,
    /// The one connection allowed to write. Batching beats parallelism on SQLite;
    /// a second writer would only add lock contention.
    writer: Mutex<Connection>,
    /// Read-only-in-role connections (opened normally: a WAL reader needs write
    /// access to the `-shm` index, so `SQLITE_OPEN_READ_ONLY` is not a reliable
    /// mode for a WAL archive).
    readers: Vec<Mutex<Connection>>,
    next_reader: AtomicUsize,
    busy_timeout_ms: u64,
    max_rows_per_statement: usize,
    metrics: Arc<Metrics>,
}

impl SqliteStorage {
    pub fn open(
        _cfg: &Config,
        backend: &BackendConfig,
        metrics: Arc<Metrics>,
    ) -> Result<Self, StorageError> {
        let path = backend.sqlite_path.clone();
        if path != ":memory:" {
            if let Some(parent) = Path::new(&path).parent() {
                if !parent.as_os_str().is_empty() {
                    std::fs::create_dir_all(parent).map_err(|err| {
                        StorageError::Config(format!(
                            "cannot create the archive directory {}: {err}",
                            parent.display()
                        ))
                    })?;
                }
            }
        }
        let writer = open_connection(&path, backend.busy_timeout_ms, true)?;
        migrate(&writer, &path)?;

        let read_pool_size = backend.read_pool_size.clamp(1, 64);
        let mut readers = Vec::with_capacity(read_pool_size);
        for _ in 0..read_pool_size {
            match open_connection(&path, backend.busy_timeout_ms, false) {
                Ok(conn) => readers.push(Mutex::new(conn)),
                Err(_) if !readers.is_empty() => break, // degrade to fewer readers
                Err(err) => return Err(err),
            }
        }

        let storage = SqliteStorage {
            path: path.clone(),
            writer: Mutex::new(writer),
            readers,
            next_reader: AtomicUsize::new(0),
            busy_timeout_ms: backend.busy_timeout_ms,
            max_rows_per_statement: backend.max_rows_per_statement.max(1),
            metrics,
        };
        storage
            .metrics
            .storage_pool_capacity
            .set(storage.readers.len() as i64 + 1);
        // Prove the archive is usable before the service advertises readiness.
        storage.probe()?;
        Ok(storage)
    }

    fn reader(&self) -> &Mutex<Connection> {
        let index = self.next_reader.fetch_add(1, Ordering::Relaxed) % self.readers.len();
        &self.readers[index]
    }

    fn timed<T>(
        &self,
        histogram: &crate::metrics::Histogram,
        operation: impl FnOnce() -> Result<T, StorageError>,
    ) -> Result<T, StorageError> {
        let started = Instant::now();
        let result = operation();
        histogram.observe_seconds(started.elapsed().as_secs_f64());
        result
    }

    /// Run one read statement and map every row.
    fn read_rows<T>(
        &self,
        plan: &query::Plan,
        map: impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<T>,
    ) -> Result<Vec<T>, StorageError> {
        let conn = self.reader();
        let conn = conn.lock().unwrap_or_else(|e| e.into_inner());
        let mut statement = conn
            .prepare_cached(&plan.sql)
            .map_err(|err| map_error(err, "prepare read"))?;
        let values = sqlite_params(&plan.params);
        let rows = statement
            .query_map(rusqlite::params_from_iter(values.iter()), map)
            .map_err(|err| map_error(err, "execute read"))?
            .collect::<rusqlite::Result<Vec<T>>>()
            .map_err(|err| map_error(err, "read row"))?;
        Ok(rows)
    }

    /// Run one scalar read (`SELECT <one column>`).
    fn read_one_i64(&self, plan: &query::Plan) -> Result<i64, StorageError> {
        let conn = self.reader();
        let conn = conn.lock().unwrap_or_else(|e| e.into_inner());
        let mut statement = conn
            .prepare_cached(&plan.sql)
            .map_err(|err| map_error(err, "prepare scalar"))?;
        let values = sqlite_params(&plan.params);
        let value = statement
            .query_row(rusqlite::params_from_iter(values.iter()), |row| {
                row.get::<_, Option<i64>>(0).map(|v| v.unwrap_or(0))
            })
            .map_err(|err| map_error(err, "read scalar"))?;
        Ok(value)
    }

    /// Run an aggregate returning `(key, count)` pairs.
    fn read_pairs(&self, plan: &query::Plan) -> Result<Vec<(String, i64)>, StorageError> {
        self.read_rows(plan, |row| {
            let key: Option<String> = row.get(0)?;
            let count: i64 = row.get(1)?;
            Ok((key.unwrap_or_default(), count))
        })
    }
}

fn open_connection(
    path: &str,
    busy_timeout_ms: u64,
    is_writer: bool,
) -> Result<Connection, StorageError> {
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|err| StorageError::Config(format!("cannot open SQLite archive {path}: {err}")))?;
    // `busy_timeout` first: every later statement depends on it. SQLite's default
    // is 0, which turns ordinary write contention into an immediate failure.
    conn.busy_timeout(std::time::Duration::from_millis(busy_timeout_ms))
        .map_err(|err| map_error(err, "set busy_timeout"))?;
    if is_writer {
        // Durability, exactly as the Python engine documents it. Setting these on
        // the writer is enough: journal_mode is a database property and
        // synchronous is per-connection, and the writer is the connection that
        // commits.
        conn.pragma_update(None, "journal_mode", "WAL")
            .map_err(|err| map_error(err, "set journal_mode"))?;
        conn.pragma_update(None, "synchronous", "FULL")
            .map_err(|err| map_error(err, "set synchronous"))?;
    }
    Ok(conn)
}

/// Create the table, add the missing v18 columns and create the index set.
///
/// Idempotent, and safe to run against an archive created by the Python engine:
/// `PRAGMA table_info` decides which `ALTER` statements are needed, exactly like
/// `logger/main.py`.
pub fn migrate(conn: &Connection, path: &str) -> Result<(), StorageError> {
    conn.execute_batch(CREATE_TABLE)
        .map_err(|err| map_error(err, "create table"))?;
    let existing = column_names(conn)?;
    for (column, ddl) in MIGRATION_COLUMNS {
        if !existing.iter().any(|name| name == column) {
            conn.execute(ddl, []).map_err(|err| {
                StorageError::Backend(format!(
                    "migration failed adding column {column} to {path}: {err}"
                ))
            })?;
        }
    }
    for (_name, ddl) in INDEXES {
        conn.execute(ddl, [])
            .map_err(|err| map_error(err, "create index"))?;
    }
    Ok(())
}

/// The columns present in `logs` right now.
pub fn column_names(conn: &Connection) -> Result<Vec<String>, StorageError> {
    let mut statement = conn
        .prepare("PRAGMA table_info(logs)")
        .map_err(|err| map_error(err, "pragma table_info"))?;
    let rows = statement
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|err| map_error(err, "pragma table_info"))?;
    let mut names = Vec::new();
    for row in rows {
        names.push(row.map_err(|err| map_error(err, "pragma row"))?);
    }
    Ok(names)
}

fn sqlite_params(params: &[Param]) -> Vec<rusqlite::types::Value> {
    params
        .iter()
        .map(|param| match param {
            Param::Int(value) => rusqlite::types::Value::Integer(*value),
            Param::Text(value) => rusqlite::types::Value::Text(value.clone()),
        })
        .collect()
}

/// Map a `rusqlite` error onto the storage error taxonomy. The distinction that
/// matters is retryable (busy/locked/IO) versus fatal: a busy archive must be
/// retried, a broken statement never can be.
fn map_error(err: rusqlite::Error, context: &str) -> StorageError {
    match &err {
        rusqlite::Error::SqliteFailure(code, message) => {
            let text = message.clone().unwrap_or_else(|| format!("{err}"));
            match code.extended_code {
                SQLITE_BUSY | SQLITE_LOCKED | SQLITE_BUSY_SNAPSHOT | SQLITE_BUSY_TIMEOUT => {
                    StorageError::Busy(format!("{context}: {text}"))
                }
                SQLITE_IOERR => StorageError::Transient(format!("{context}: {text}")),
                // Disk-full is a retryable environment condition: ingest keeps
                // refusing, producers keep their records, and the recovery is
                // free space - not a code change.
                SQLITE_FULL | SQLITE_IOERR_WRITE | SQLITE_IOERR_FSYNC => {
                    StorageError::Transient(format!("{context}: disk-full: {text}"))
                }
                _ => StorageError::Backend(format!("{context}: {text}")),
            }
        }
        _ => StorageError::Backend(format!("{context}: {err}")),
    }
}

fn row_to_record(row: &rusqlite::Row<'_>) -> rusqlite::Result<Record> {
    Ok(Record {
        id: row.get("id")?,
        ts: row.get("ts")?,
        level: row.get::<_, Option<String>>("level")?.unwrap_or_default(),
        service: row.get::<_, Option<String>>("service")?.unwrap_or_default(),
        category: row
            .get::<_, Option<String>>("category")?
            .unwrap_or_default(),
        message: row.get::<_, Option<String>>("message")?.unwrap_or_default(),
        detail: row.get::<_, Option<String>>("detail")?.unwrap_or_default(),
        rule_id: row.get::<_, Option<String>>("rule_id")?.unwrap_or_default(),
        user_id: row.get::<_, Option<i64>>("user_id")?.unwrap_or(0),
        correlation_id: row
            .get::<_, Option<String>>("correlation_id")?
            .unwrap_or_default(),
        // Read through `Option` so a row selected by an older statement (or a
        // hand-built archive) cannot fail the whole page: an absent structured
        // column means "no context", not "broken row".
        request_id: row
            .get::<_, Option<String>>("request_id")?
            .unwrap_or_default(),
        trace_id: row
            .get::<_, Option<String>>("trace_id")?
            .unwrap_or_default(),
        module: row.get::<_, Option<String>>("module")?.unwrap_or_default(),
        fingerprint: row
            .get::<_, Option<String>>("fingerprint")?
            .unwrap_or_default(),
    })
}

impl Storage for SqliteStorage {
    fn backend(&self) -> Backend {
        Backend::Sqlite
    }

    fn insert_batch(&self, records: &[Record]) -> Result<usize, StorageError> {
        if records.is_empty() {
            return Ok(0);
        }
        let started = Instant::now();
        let mut conn = self.writer.lock().unwrap_or_else(|e| e.into_inner());
        // BEGIN IMMEDIATE takes the write lock up front, so the failure mode is a
        // bounded busy_timeout wait rather than a deferred lock upgrade deadlock.
        let transaction = conn
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|err| map_error(err, "begin transaction"))?;
        let mut written = 0usize;
        for chunk in records.chunks(self.max_rows_per_statement) {
            let sql = insert_sql(chunk.len());
            let mut statement = transaction
                .prepare(&sql)
                .map_err(|err| map_error(err, "prepare insert"))?;
            let mut values: Vec<rusqlite::types::Value> = Vec::with_capacity(chunk.len() * 13);
            for record in chunk {
                values.push(rusqlite::types::Value::Integer(record.ts));
                values.push(rusqlite::types::Value::Text(record.level.clone()));
                values.push(rusqlite::types::Value::Text(record.service.clone()));
                values.push(rusqlite::types::Value::Text(record.category.clone()));
                values.push(rusqlite::types::Value::Text(record.message.clone()));
                values.push(rusqlite::types::Value::Text(record.detail.clone()));
                values.push(rusqlite::types::Value::Text(record.rule_id.clone()));
                values.push(rusqlite::types::Value::Integer(record.user_id));
                values.push(rusqlite::types::Value::Text(record.correlation_id.clone()));
                values.push(rusqlite::types::Value::Text(record.request_id.clone()));
                values.push(rusqlite::types::Value::Text(record.trace_id.clone()));
                values.push(rusqlite::types::Value::Text(record.module.clone()));
                values.push(rusqlite::types::Value::Text(record.fingerprint.clone()));
            }
            let affected = statement
                .execute(rusqlite::params_from_iter(values.iter()))
                .map_err(|err| map_error(err, "insert batch"))?;
            written += affected;
        }
        transaction
            .commit()
            .map_err(|err| map_error(err, "commit batch"))?;
        self.metrics
            .storage_write_seconds
            .observe_seconds(started.elapsed().as_secs_f64());
        Ok(written)
    }

    fn query(&self, filter: &QueryFilter, page: &Page) -> Result<(Vec<Record>, i64), StorageError> {
        let select = query::select_page(filter, page, SqlDialect::Sqlite);
        let rows = self.timed(&self.metrics.storage_read_seconds, || {
            self.read_rows(&select, row_to_record)
        })?;
        // Fast path: when the first page is not full there cannot be more matches,
        // so the count is the number of rows we already hold. This is the common
        // case for "show me the recent N" and it avoids a full-table COUNT
        // entirely. (It is also exactly the answer the Python engine's COUNT
        // produced; it is just cheaper.)
        if page.offset == 0 && rows.len() < page.limit {
            let seen = rows.len() as i64;
            return Ok((rows, seen));
        }
        let count_plan = query::count(filter, None, SqlDialect::Sqlite);
        let total = self.timed(&self.metrics.storage_read_seconds, || {
            self.read_one_i64(&count_plan)
        })?;
        Ok((rows, total))
    }

    fn count(&self, filter: &QueryFilter, cap: Option<i64>) -> Result<i64, StorageError> {
        let plan = query::count(filter, cap, SqlDialect::Sqlite);
        self.timed(&self.metrics.storage_read_seconds, || {
            self.read_one_i64(&plan)
        })
    }

    /// The write-ahead journal lives next to the archive as `<path>-wal`
    /// whenever the connection is in WAL mode; the same connection that set it
    /// up is the one that can read its size.
    fn wal_size_bytes(&self) -> i64 {
        std::fs::metadata(format!("{}-wal", self.path))
            .map(|m| m.len() as i64)
            .unwrap_or(0)
    }

    fn table_rows(&self) -> Result<i64, StorageError> {
        let plan = query::count(&QueryFilter::default(), None, SqlDialect::Sqlite);
        self.timed(&self.metrics.storage_read_seconds, || {
            self.read_one_i64(&plan)
        })
    }

    fn stats(&self, scope: &StatsScope) -> Result<Stats, StorageError> {
        let scoped = QueryFilter {
            rule_id: scope.rule_id.clone(),
            user_id: scope.user_id,
            ..QueryFilter::default()
        };
        let aggregate = query::stats(scope, SqlDialect::Sqlite);
        let (total, oldest_ts, newest_ts) = {
            let conn = self.reader();
            let conn = conn.lock().unwrap_or_else(|e| e.into_inner());
            let mut statement = conn
                .prepare_cached(&aggregate.sql)
                .map_err(|err| map_error(err, "prepare stats"))?;
            let values = sqlite_params(&aggregate.params);
            statement
                .query_row(rusqlite::params_from_iter(values.iter()), |row| {
                    Ok((
                        row.get::<_, Option<i64>>(0)?.unwrap_or(0),
                        row.get::<_, Option<i64>>(1)?.unwrap_or(0),
                        row.get::<_, Option<i64>>(2)?.unwrap_or(0),
                    ))
                })
                .map_err(|err| map_error(err, "read stats"))?
        };

        let mut stats = Stats {
            total,
            oldest_ts,
            newest_ts,
            ..Stats::default()
        };
        for (by, target) in [
            (GroupBy::Level, GroupTarget::Level),
            (GroupBy::Category, GroupTarget::Category),
            (GroupBy::Service, GroupTarget::Service),
        ] {
            let plan = query::group_by(&scoped, by, MAX_GROUPS, SqlDialect::Sqlite);
            let pairs = self.timed(&self.metrics.storage_read_seconds, || {
                self.read_pairs(&plan)
            })?;
            for (key, value) in pairs {
                let slot = match target {
                    GroupTarget::Level => stats.by_level.entry(key),
                    GroupTarget::Category => stats.by_category.entry(key),
                    GroupTarget::Service => stats.by_service.entry(key),
                };
                slot.or_insert(value);
            }
        }
        // `by_rule` counts the whole archive, not the scoped slice: "which task is
        // noisy?" stays answerable from a single task's view. Exactly the Python
        // engine's behaviour.
        let by_rule = query::by_rule(MAX_RULE_GROUPS, SqlDialect::Sqlite);
        for (key, value) in self.timed(&self.metrics.storage_read_seconds, || {
            self.read_pairs(&by_rule)
        })? {
            stats.by_rule.insert(key, value);
        }
        Ok(stats)
    }

    fn top_groups(
        &self,
        filter: &QueryFilter,
        by: GroupBy,
        limit: usize,
    ) -> Result<Vec<(String, i64)>, StorageError> {
        let plan = query::group_by(filter, by, limit.min(MAX_GROUPS), SqlDialect::Sqlite);
        self.timed(&self.metrics.storage_read_seconds, || {
            self.read_pairs(&plan)
        })
    }

    fn error_aggregates(
        &self,
        filter: &QueryFilter,
        limit: usize,
    ) -> Result<Vec<ErrorAgg>, StorageError> {
        let plan = query::error_aggregates(filter, limit, SqlDialect::Sqlite);
        self.timed(&self.metrics.storage_read_seconds, || {
            self.read_rows(&plan, |row| {
                Ok(ErrorAgg {
                    fingerprint: row.get::<_, Option<String>>(0)?.unwrap_or_default(),
                    level: row.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    service: row.get::<_, Option<String>>(2)?.unwrap_or_default(),
                    category: row.get::<_, Option<String>>(3)?.unwrap_or_default(),
                    module: row.get::<_, Option<String>>(4)?.unwrap_or_default(),
                    message: row.get::<_, Option<String>>(5)?.unwrap_or_default(),
                    count: row.get(6)?,
                    first_ts: row.get::<_, Option<i64>>(7)?.unwrap_or(0),
                    last_ts: row.get::<_, Option<i64>>(8)?.unwrap_or(0),
                })
            })
        })
    }

    fn time_buckets(
        &self,
        filter: &QueryFilter,
        bucket_secs: i64,
        max_buckets: usize,
    ) -> Result<Vec<Bucket>, StorageError> {
        let plan = query::time_buckets(filter, bucket_secs, max_buckets, SqlDialect::Sqlite);
        self.timed(&self.metrics.storage_read_seconds, || {
            self.read_rows(&plan, |row| {
                Ok(Bucket {
                    start_ts: row.get::<_, Option<i64>>(0)?.unwrap_or(0),
                    count: row.get(1)?,
                })
            })
        })
    }

    fn purge(
        &self,
        filter: &PurgeFilter,
        chunk: usize,
        max_rows: u64,
    ) -> Result<PurgeReport, StorageError> {
        let started = Instant::now();
        let cutoff = filter.cutoff();
        let scoped = QueryFilter {
            level: filter.level.trim().to_string(),
            service: filter.service.trim().to_string(),
            category: filter.category.trim().to_string(),
            rule_id: filter.rule_id.trim().to_string(),
            ..QueryFilter::default()
        };
        let protected = normalise_categories(&filter.protect_categories);
        let mut purged: i64 = 0;
        let mut exhausted = false;
        {
            let mut writer = self.writer.lock().unwrap_or_else(|e| e.into_inner());
            let transaction = writer
                .transaction()
                .map_err(|err| map_error(err, "begin purge"))?;
            loop {
                let remaining_budget = max_rows.saturating_sub(purged as u64);
                if remaining_budget == 0 {
                    exhausted = true;
                    break;
                }
                let take = chunk.min(remaining_budget as usize).max(1);
                // The protected-category guard is part of the same statement that
                // chooses which rows to delete: an audit row cannot be removed by a
                // broad retention rule, only by an explicit purge that names its
                // category.
                let plan =
                    query::delete_chunk(&scoped, cutoff, &protected, take, SqlDialect::Sqlite);
                let mut statement = transaction
                    .prepare(&plan.sql)
                    .map_err(|err| map_error(err, "prepare purge chunk"))?;
                let values = sqlite_params(&plan.params);
                let deleted = statement
                    .execute(rusqlite::params_from_iter(values.iter()))
                    .map_err(|err| map_error(err, "purge chunk"))?;
                purged += deleted as i64;
                if deleted == 0 {
                    break;
                }
            }
            transaction
                .commit()
                .map_err(|err| map_error(err, "commit purge"))?;

            // A purge is the one place this engine checkpoints the WAL: it is what
            // actually gives the disk space back, and it is exactly where the Python
            // engine puts it (never on the ingest path).
            let _ = writer.execute_batch("PRAGMA wal_checkpoint(PASSIVE)");
        }

        let mut protected_skipped = 0i64;
        if !protected.is_empty() {
            let plan = query::count_protected_candidates(
                &scoped,
                cutoff,
                &protected,
                1_000_000,
                SqlDialect::Sqlite,
            );
            protected_skipped = self.read_one_i64(&plan).unwrap_or(0);
            self.metrics
                .retention_protected_rows_skipped
                .add(protected_skipped.max(0) as u64);
        }
        // `remaining` is the whole table, matching the Python engine's response.
        let remaining = self.table_rows()?;
        self.metrics.storage_rows.set(remaining);
        self.metrics
            .storage_write_seconds
            .observe_seconds(started.elapsed().as_secs_f64());
        Ok(PurgeReport {
            purged,
            remaining,
            exhausted,
            protected_skipped: protected_skipped > 0,
        })
    }

    fn export_chunk(&self, filter: &QueryFilter, page: &Page) -> Result<Vec<Record>, StorageError> {
        let plan = query::select_page(filter, page, SqlDialect::Sqlite);
        self.timed(&self.metrics.storage_read_seconds, || {
            self.read_rows(&plan, row_to_record)
        })
    }

    /// One bounded retention sweep (see [`RetentionSweep`]).
    fn retention_purge(
        &self,
        sweep: &RetentionSweep,
        chunk: usize,
        max_rows: u64,
    ) -> Result<PurgeReport, StorageError> {
        let started = Instant::now();
        let protected = normalise_categories(&sweep.protected);
        let effective = RetentionSweep {
            protected: protected.clone(),
            ..sweep.clone()
        };
        let mut purged: i64 = 0;
        let mut exhausted = false;
        {
            let mut writer = self.writer.lock().unwrap_or_else(|e| e.into_inner());
            let transaction = writer
                .transaction()
                .map_err(|err| map_error(err, "begin retention sweep"))?;
            loop {
                let remaining_budget = max_rows.saturating_sub(purged as u64);
                if remaining_budget == 0 {
                    exhausted = true;
                    break;
                }
                let take = chunk.min(remaining_budget as usize).max(1);
                let plan = query::retention_delete_chunk(&effective, take, SqlDialect::Sqlite);
                let mut statement = transaction
                    .prepare(&plan.sql)
                    .map_err(|err| map_error(err, "prepare retention chunk"))?;
                let values = sqlite_params(&plan.params);
                let deleted = statement
                    .execute(rusqlite::params_from_iter(values.iter()))
                    .map_err(|err| map_error(err, "retention chunk"))?;
                purged += deleted as i64;
                if deleted == 0 {
                    break;
                }
            }
            transaction
                .commit()
                .map_err(|err| map_error(err, "commit retention sweep"))?;
        }
        let remaining = self.table_rows()?;
        self.metrics.storage_rows.set(remaining);
        self.metrics
            .storage_write_seconds
            .observe_seconds(started.elapsed().as_secs_f64());
        Ok(PurgeReport {
            purged,
            remaining,
            exhausted,
            protected_skipped: !protected.is_empty(),
        })
    }

    fn probe(&self) -> Result<(), StorageError> {
        let started = Instant::now();
        let result = (|| -> Result<(), StorageError> {
            let conn = self.reader();
            let conn = conn.lock().unwrap_or_else(|e| e.into_inner());
            // Touching `logs` proves the schema is present *and* that the file is
            // readable — a bare `SELECT 1` would pass on an empty, un-migrated file.
            conn.query_row("SELECT 1 FROM logs LIMIT 1", [], |_| Ok(()))
                .or_else(|err| match err {
                    // No rows is a healthy archive, not a failure.
                    rusqlite::Error::QueryReturnedNoRows => Ok(()),
                    other => Err(other),
                })
                .map_err(|err| map_error(err, "readiness probe"))?;
            Ok(())
        })();
        self.metrics
            .storage_read_seconds
            .observe_seconds(started.elapsed().as_secs_f64());
        if result.is_err() {
            self.metrics.storage_probe_failures.inc();
        }
        result
    }

    fn checkpoint(&self) -> Result<(), StorageError> {
        let conn = self.writer.lock().unwrap_or_else(|e| e.into_inner());
        conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE)")
            .map_err(|err| map_error(err, "wal_checkpoint"))?;
        Ok(())
    }

    fn describe(&self) -> String {
        format!(
            "sqlite path={} readers={} busy_timeout_ms={} rows_per_statement={}",
            self.path,
            self.readers.len(),
            self.busy_timeout_ms,
            self.max_rows_per_statement
        )
    }
}

enum GroupTarget {
    Level,
    Category,
    Service,
}

/// `INSERT INTO logs (...) VALUES (...), (...), ...` for `rows` rows.
pub fn insert_sql(rows: usize) -> String {
    let mut sql = String::from(
        "INSERT INTO logs (ts, level, service, category, message, detail, rule_id, user_id, \
         correlation_id, request_id, trace_id, module, fingerprint) VALUES ",
    );
    for index in 0..rows {
        if index > 0 {
            sql.push_str(", ");
        }
        sql.push_str("(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)");
    }
    sql
}

fn normalise_categories(categories: &[String]) -> Vec<String> {
    let mut out: Vec<String> = categories
        .iter()
        .map(|c| c.trim().to_uppercase())
        .filter(|c| !c.is_empty())
        .collect();
    out.sort();
    out.dedup();
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::query::Keyset;
    use std::path::PathBuf;

    struct TempArchive {
        /// Held only so the directory outlives the test.
        _dir: tempfile::TempDir,
        path: PathBuf,
    }

    impl TempArchive {
        fn new() -> Self {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("atf_logs.db");
            TempArchive { _dir: dir, path }
        }

        fn storage(&self) -> (Arc<SqliteStorage>, Arc<Metrics>) {
            self.storage_with(2, 500)
        }

        fn storage_with(
            &self,
            readers: usize,
            rows_per_statement: usize,
        ) -> (Arc<SqliteStorage>, Arc<Metrics>) {
            let cfg = Config {
                read_pool_size: readers,
                ..Default::default()
            };
            let backend = BackendConfig {
                backend: Backend::Sqlite,
                sqlite_path: self.path.to_string_lossy().to_string(),
                postgres_dsn: None,
                busy_timeout_ms: 5_000,
                read_pool_size: readers,
                pool_timeout_ms: 5_000,
                statement_timeout_ms: 0,
                transaction_timeout_ms: 0,
                max_rows_per_statement: rows_per_statement,
            };
            let metrics = Arc::new(Metrics::new());
            let storage =
                Arc::new(SqliteStorage::open(&cfg, &backend, Arc::clone(&metrics)).unwrap());
            (storage, metrics)
        }

        fn sqlite_path(&self) -> String {
            self.path.to_string_lossy().to_string()
        }
    }

    fn record(ts: i64, level: &str, message: &str) -> Record {
        Record {
            id: 0,
            ts,
            level: level.to_string(),
            service: "core".to_string(),
            category: "system".to_string(),
            message: message.to_string(),
            detail: "{\"rule_id\":\"r1\"}".to_string(),
            rule_id: "rule-1".to_string(),
            user_id: 42,
            correlation_id: "corr-1".to_string(),
            request_id: String::new(),
            trace_id: String::new(),
            module: String::new(),
            fingerprint: String::new(),
        }
    }

    #[test]
    fn schema_matches_the_python_engine() {
        let archive = TempArchive::new();
        let (storage, _metrics) = archive.storage();
        let conn = Connection::open(archive.sqlite_path()).unwrap();
        let columns = column_names(&conn).unwrap();
        // The v19 archive carries the structured-context columns on top of the
        // ten the Python engine laid down: additive `ADD COLUMN ... DEFAULT ''`
        // migration, so an old archive grows them in place.
        assert_eq!(
            columns,
            vec![
                "id",
                "ts",
                "level",
                "service",
                "category",
                "message",
                "detail",
                "rule_id",
                "user_id",
                "correlation_id",
                "request_id",
                "trace_id",
                "module",
                "fingerprint"
            ]
        );
        let journal: String = conn
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .unwrap();
        assert_eq!(journal.to_lowercase(), "wal");
        let indexes: Vec<String> = {
            let mut statement = conn
                .prepare("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='logs' ORDER BY name")
                .unwrap();
            let rows = statement
                .query_map([], |row| row.get::<_, String>(0))
                .unwrap()
                .collect::<rusqlite::Result<Vec<_>>>()
                .unwrap();
            rows
        };
        for (name, _) in INDEXES {
            assert!(
                indexes.contains(&name.to_string()),
                "missing index {name}: {indexes:?}"
            );
        }
        assert!(storage.describe().contains("sqlite"));
    }

    #[test]
    fn an_old_archive_is_migrated_in_place_with_the_structured_context_columns() {
        let archive = TempArchive::new();
        {
            let conn = Connection::open(archive.sqlite_path()).unwrap();
            conn.execute_batch(CREATE_TABLE).unwrap();
            conn.execute(
                "ALTER TABLE logs ADD COLUMN rule_id TEXT NOT NULL DEFAULT ''",
                [],
            )
            .unwrap();
            conn.execute(
                "ALTER TABLE logs ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0",
                [],
            )
            .unwrap();
            conn.execute(
                "ALTER TABLE logs ADD COLUMN correlation_id TEXT NOT NULL DEFAULT ''",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO logs (ts, level, service, category, message, detail) \
                 VALUES (?, ?, ?, ?, ?, ?)",
                rusqlite::params![1i64, "ERROR", "core", "system", "old row", ""],
            )
            .unwrap();
        }
        // Opening the archive runs the v19 ADD COLUMN migration: no rebuild, no
        // copy, and the pre-existing row reads back with empty context fields.
        let (storage, _metrics) = archive.storage();
        let (rows, _total) = storage
            .query(&QueryFilter::default(), &Page::clamped(10, 0, true))
            .unwrap();
        assert_eq!(rows.len(), 1);
        let row = &rows[0];
        assert_eq!(row.request_id, "");
        assert_eq!(row.trace_id, "");
        assert_eq!(row.module, "");
        // A stored fingerprint exists even for a legacy row: it is computed at
        // ingest for new records, and the migration leaves the column default
        // empty rather than backfilling a value that cannot be honest about its
        // derivation time.
        assert_eq!(row.fingerprint, "");
    }

    #[test]
    fn migration_reads_an_existing_python_archive() {
        let archive = TempArchive::new();
        // Build the archive exactly as `logger/main.py` would: base table, then the
        // v18 ALTERs (NOT the ones this engine added later — the point is that the
        // v18 shape opens in place), then the Python index set, then a row.
        {
            let conn = Connection::open(archive.sqlite_path()).unwrap();
            conn.execute_batch(CREATE_TABLE).unwrap();
            for ddl in [
                "ALTER TABLE logs ADD COLUMN rule_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE logs ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE logs ADD COLUMN correlation_id TEXT NOT NULL DEFAULT ''",
            ] {
                conn.execute(ddl, []).unwrap();
            }
            for ddl in [
                "CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)",
                "CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)",
                "CREATE INDEX IF NOT EXISTS idx_logs_category ON logs(category)",
                "CREATE INDEX IF NOT EXISTS idx_logs_service ON logs(service)",
                "CREATE INDEX IF NOT EXISTS idx_logs_rule ON logs(rule_id)",
                "CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_logs_corr ON logs(correlation_id)",
            ] {
                conn.execute(ddl, []).unwrap();
            }
            conn.execute(
                "INSERT INTO logs (ts, level, service, category, message, detail, rule_id, user_id, correlation_id) \
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rusqlite::params![
                    1_700_000_001i64,
                    "WARNING",
                    "core",
                    "system",
                    "legacy row",
                    "{\"a\":1}",
                    "old-rule",
                    7i64,
                    "old-corr"
                ],
            )
            .unwrap();
        }

        // The Rust engine adopts it in place: no rebuild, no data loss, no duplicate
        // index, and the pre-canonicalisation level stays readable.
        let (storage, _metrics) = archive.storage();
        let (rows, total) = storage
            .query(&QueryFilter::default(), &Page::default())
            .unwrap();
        assert_eq!(total, 1);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].level, "WARNING");
        assert_eq!(rows[0].message, "legacy row");
        assert_eq!(rows[0].detail, "{\"a\":1}");
        assert_eq!(rows[0].rule_id, "old-rule");
        assert_eq!(rows[0].user_id, 7);
        assert_eq!(rows[0].correlation_id, "old-corr");

        // And the new engine writes into the same archive.
        storage
            .insert_batch(&[record(1_700_000_002, "ERROR", "new row")])
            .unwrap();
        assert_eq!(storage.table_rows().unwrap(), 2);

        // No index was created twice under a different name.
        let conn = Connection::open(archive.sqlite_path()).unwrap();
        let indexes: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND tbl_name='logs'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(indexes, INDEXES.len() as i64);
    }

    #[test]
    fn insert_batch_splits_and_commits_atomically() {
        let archive = TempArchive::new();
        // 3 rows per statement forces the batch of 10 to be split into 4 statements
        // inside one transaction.
        let (storage, metrics) = archive.storage_with(1, 3);
        let batch: Vec<Record> = (0..10)
            .map(|i| record(1_700_000_000 + i, "ERROR", &format!("row {i}")))
            .collect();
        let written = storage.insert_batch(&batch).unwrap();
        assert_eq!(written, 10);
        assert_eq!(storage.table_rows().unwrap(), 10);
        assert_eq!(
            metrics.events_persisted.get(),
            0,
            "storage must not count events"
        );
        assert_eq!(metrics.storage_write_seconds.count(), 1);
        // Ids are assigned by the database and are increasing.
        let (rows, _) = storage
            .query(
                &QueryFilter::default(),
                &Page {
                    ascending: true,
                    ..Page::default()
                },
            )
            .unwrap();
        assert!(rows.windows(2).all(|w| w[0].id < w[1].id));
    }

    #[test]
    fn query_filters_search_pagination_and_counts_behave_like_python() {
        let archive = TempArchive::new();
        let (storage, _metrics) = archive.storage();
        let mut batch = Vec::new();
        for index in 0..25 {
            let mut row = record(
                1_700_000_000 + index,
                if index % 5 == 0 { "ERROR" } else { "INFO" },
                &format!("event {index}"),
            );
            row.category = if index % 2 == 0 {
                "system".to_string()
            } else {
                "forward".to_string()
            };
            row.detail = format!("{{\"seq\":{index},\"correlation_id\":\"corr-{index}\"}}");
            row.correlation_id = format!("corr-{index}");
            batch.push(row);
        }
        storage.insert_batch(&batch).unwrap();

        // Newest first, with the exact total.
        let (rows, total) = storage
            .query(&QueryFilter::default(), &Page::default())
            .unwrap();
        assert_eq!(total, 25);
        assert_eq!(rows.len(), 25);
        assert!(rows[0].ts > rows[1].ts);

        // Multi-level filter wins over the single level.
        let filter = QueryFilter {
            levels: vec!["error".to_string()],
            level: "info".to_string(),
            ..QueryFilter::default()
        };
        let (errors, total) = storage.query(&filter, &Page::default()).unwrap();
        assert_eq!(total, 5);
        assert!(errors.iter().all(|r| r.level == "ERROR"));

        // The single-level path upper-cases, exactly like the Python engine.
        let filter = QueryFilter {
            level: "warn".to_string(),
            ..QueryFilter::default()
        };
        assert_eq!(storage.query(&filter, &Page::default()).unwrap().1, 0);

        // Substring search spans message and detail.
        let filter = QueryFilter {
            search: "corr-13".to_string(),
            ..QueryFilter::default()
        };
        assert_eq!(storage.query(&filter, &Page::default()).unwrap().1, 1);
        let filter = QueryFilter {
            search: "event 1".to_string(),
            ..QueryFilter::default()
        };
        assert_eq!(storage.query(&filter, &Page::default()).unwrap().1, 11);

        // Offset paging walks the archive; the total is the whole match set.
        let page = Page {
            limit: 10,
            offset: 20,
            ascending: false,
            keyset: None,
        };
        let (rows, total) = storage.query(&QueryFilter::default(), &page).unwrap();
        assert_eq!(total, 25);
        assert_eq!(rows.len(), 5);

        // Keyset paging skips exactly one row.
        let (first, _) = storage
            .query(
                &QueryFilter::default(),
                &Page {
                    limit: 5,
                    ..Page::default()
                },
            )
            .unwrap();
        let cursor = Keyset::from_record(first.last().unwrap());
        let (next, _) = storage
            .query(
                &QueryFilter::default(),
                &Page {
                    limit: 5,
                    ascending: false,
                    keyset: Some(cursor),
                    ..Page::default()
                },
            )
            .unwrap();
        assert_eq!(next.len(), 5);
        assert!(next
            .iter()
            .all(|row| (row.ts, row.id) < (cursor.ts, cursor.id)));

        // Time range.
        let filter = QueryFilter {
            since: 1_700_000_010,
            until: 1_700_000_014,
            ..QueryFilter::default()
        };
        assert_eq!(storage.query(&filter, &Page::default()).unwrap().1, 5);

        // Correlation and user scoping.
        let filter = QueryFilter {
            correlation_id: "corr-3".to_string(),
            user_id: 42,
            ..QueryFilter::default()
        };
        assert_eq!(storage.query(&filter, &Page::default()).unwrap().1, 1);
        let filter = QueryFilter {
            user_id: 43,
            ..QueryFilter::default()
        };
        assert_eq!(storage.query(&filter, &Page::default()).unwrap().1, 0);

        // Bounded observation count.
        assert_eq!(
            storage.count(&QueryFilter::default(), Some(10)).unwrap(),
            10
        );
        assert_eq!(storage.count(&QueryFilter::default(), None).unwrap(), 25);
    }

    #[test]
    fn stats_aggregates_match_the_python_engine() {
        let archive = TempArchive::new();
        let (storage, _metrics) = archive.storage();
        let mut batch = Vec::new();
        for index in 0..12 {
            let mut row = record(
                1_700_000_100 + index,
                if index % 3 == 0 { "ERROR" } else { "INFO" },
                "m",
            );
            row.service = if index % 2 == 0 {
                "core".to_string()
            } else {
                "api".to_string()
            };
            row.rule_id = if index < 6 {
                "rule-a".to_string()
            } else {
                String::new()
            };
            row.user_id = if index < 4 { 1 } else { 2 };
            // One signature for every row: category/module/message identical and
            // severity/service deliberately ignored (LEVEL and SERVICE are
            // reported, not partitioned -- see ingest::fingerprint_of).
            row.fingerprint = "system||m".to_string();
            batch.push(row);
        }
        storage.insert_batch(&batch).unwrap();
        let stats = storage.stats(&StatsScope::default()).unwrap();
        assert_eq!(stats.total, 12);
        assert_eq!(stats.by_level.get("ERROR"), Some(&4));
        assert_eq!(stats.by_level.get("INFO"), Some(&8));
        assert_eq!(stats.by_service.get("core"), Some(&6));
        assert_eq!(stats.by_rule.get("rule-a"), Some(&6));
        assert_eq!(stats.oldest_ts, 1_700_000_100);
        assert_eq!(stats.newest_ts, 1_700_000_111);

        // Scoped stats: by_rule still covers the whole archive.
        let scoped = storage
            .stats(&StatsScope {
                rule_id: String::new(),
                user_id: 1,
            })
            .unwrap();
        assert_eq!(scoped.total, 4);
        assert_eq!(scoped.by_rule.get("rule-a"), Some(&6));

        // Top groups and error aggregates.
        let top = storage
            .top_groups(&QueryFilter::default(), GroupBy::Service, 10)
            .unwrap();
        assert_eq!(top.first().map(|p| p.0.clone()), Some("core".to_string()));
        let with_errors = storage
            .top_groups(
                &QueryFilter {
                    levels: vec!["ERROR".to_string()],
                    ..QueryFilter::default()
                },
                GroupBy::Level,
                10,
            )
            .unwrap();
        assert_eq!(with_errors, vec![("ERROR".to_string(), 4)]);
        // Recurring-error groups only look at failure levels by default (the
        // 8 INFO rows are out of scope for the *aggregate*), but severity does
        // not partition within a signature.
        let aggregates = storage
            .error_aggregates(&QueryFilter::default(), 10)
            .unwrap();
        assert_eq!(aggregates.len(), 1, "{aggregates:?}");
        assert_eq!(aggregates[0].count, 4);
        assert_eq!(aggregates[0].message, "m");
        assert_eq!(aggregates[0].level, "ERROR");

        // Time buckets.
        let buckets = storage
            .time_buckets(&QueryFilter::default(), 10, 64)
            .unwrap();
        assert!(!buckets.is_empty());
        let summed: i64 = buckets.iter().map(|b| b.count).sum();
        assert_eq!(summed, 12);
    }

    #[test]
    fn recurring_errors_collapse_across_severities() {
        // Regression: identical messages that differ only in severity are ONE
        // recurring failure. Grouping the aggregate by level reported the fixture
        // below as two groups of 3, each half the real count, with the older row's
        // level attached to the newer occurrences.
        let archive = TempArchive::new();
        let (storage, _metrics) = archive.storage();
        let mut batch = Vec::new();
        for index in 0..6 {
            let mut row = record(
                1_700_000_000 + index,
                if index < 3 { "ERROR" } else { "WARN" },
                "flaky publish",
            );
            row.category = "publish".to_string();
            row.fingerprint = "publish|flaky publish".to_string();
            batch.push(row);
        }
        // Same message, different category: a different signature, so its own group
        // (the aggregate reports the category, it cannot report an arbitrary one).
        let mut other_category = record(1_700_000_100, "ERROR", "flaky publish");
        other_category.category = "billing".to_string();
        other_category.fingerprint = "billing|flaky publish".to_string();
        batch.push(other_category);
        // A different message in the same category is a different failure too.
        let mut other_message = record(1_700_000_200, "ERROR", "timeout");
        other_message.category = "publish".to_string();
        other_message.fingerprint = "publish|timeout".to_string();
        batch.push(other_message);
        storage.insert_batch(&batch).unwrap();

        let aggregates = storage
            .error_aggregates(&QueryFilter::default(), 10)
            .unwrap();
        assert_eq!(aggregates.len(), 3, "{aggregates:?}");

        let flaky = aggregates
            .iter()
            .find(|agg| agg.category == "publish" && agg.message == "flaky publish")
            .unwrap_or_else(|| panic!("missing publish/flaky publish: {aggregates:?}"));
        assert_eq!(flaky.count, 6, "all six occurrences, both severities");
        assert_eq!(flaky.last_ts, 1_700_000_005);
        assert_eq!(
            flaky.level, "WARN",
            "the newest occurrence's level, i.e. the row last_ts points at"
        );
        assert_eq!(
            aggregates
                .iter()
                .find(|agg| agg.category == "billing")
                .map(|agg| (agg.message.as_str(), agg.count)),
            Some(("flaky publish", 1))
        );
    }

    #[test]
    fn purge_is_bounded_protects_audit_and_checkpoints() {
        let archive = TempArchive::new();
        let (storage, _metrics) = archive.storage();
        let now = 1_700_000_000;
        let mut batch = Vec::new();
        for index in 0..10 {
            let mut row = record(now - 100_000 + index, "DEBUG", "old");
            row.category = "system".to_string();
            batch.push(row);
        }
        for index in 0..4 {
            let mut row = record(now - 100_000 + index, "DEBUG", "audit row");
            row.category = "audit".to_string();
            batch.push(row);
        }
        storage.insert_batch(&batch).unwrap();
        assert_eq!(storage.table_rows().unwrap(), 14);

        // Age-based purge with the audit category protected: only the 10 system rows
        // are candidates, and the protected ones are reported, not silent.
        let report = storage
            .purge(
                &PurgeFilter {
                    older_than_hours: 1,
                    protect_categories: vec!["audit".to_string()],
                    now,
                    ..PurgeFilter::default()
                },
                3,
                1_000,
            )
            .unwrap();
        assert_eq!(report.purged, 10);
        assert_eq!(report.remaining, 4);
        assert!(!report.exhausted);
        assert!(report.protected_skipped);

        // An explicit category purge can still remove audit rows: the protection is
        // a default, not a lock.
        let report = storage
            .purge(
                &PurgeFilter {
                    category: "audit".to_string(),
                    older_than_hours: 1,
                    protect_categories: Vec::new(),
                    now,
                    ..PurgeFilter::default()
                },
                2,
                1_000,
            )
            .unwrap();
        assert_eq!(report.purged, 4);
        assert_eq!(report.remaining, 0);

        // The row budget stops the work and says so.
        let mut batch = Vec::new();
        for index in 0..6 {
            batch.push(record(now - 100_000 + index, "DEBUG", "again"));
        }
        storage.insert_batch(&batch).unwrap();
        let report = storage
            .purge(
                &PurgeFilter {
                    older_than_hours: 1,
                    now,
                    ..PurgeFilter::default()
                },
                2,
                4,
            )
            .unwrap();
        assert_eq!(report.purged, 4);
        assert!(report.exhausted);
        assert_eq!(report.remaining, 2);
    }

    #[test]
    fn concurrent_reads_and_writes_coexist() {
        let archive = TempArchive::new();
        let (storage, _metrics) = archive.storage();
        let writers = Arc::clone(&storage);
        let reader = Arc::clone(&storage);
        let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));

        let read_thread = {
            let stop = Arc::clone(&stop);
            std::thread::spawn(move || {
                let mut reads = 0u64;
                while !stop.load(Ordering::Relaxed) {
                    let _ = reader
                        .query(&QueryFilter::default(), &Page::default())
                        .unwrap();
                    reads += 1;
                }
                reads
            })
        };
        for round in 0..8 {
            let batch: Vec<Record> = (0..50)
                .map(|i| record(1_700_000_000 + round * 50 + i, "WARN", "concurrent"))
                .collect();
            writers.insert_batch(&batch).unwrap();
        }
        stop.store(true, Ordering::Relaxed);
        let reads = read_thread.join().unwrap();
        assert!(reads > 0);
        assert_eq!(storage.table_rows().unwrap(), 400);
    }

    #[test]
    fn every_statement_executes_for_every_filter_combination() {
        // Regression for the parameter/placeholder mismatch: `time_buckets` emitted
        // the bucket width as one bound value in two `?` positions, so SQLite
        // refused the statement with `Wrong number of parameters passed to query.
        // Got 2, needed 3` before it ever reached the archive. Every read this
        // engine can build must execute for every filter shape, and the filtered
        // aggregates must still add up.
        let archive = TempArchive::new();
        let (storage, _metrics) = archive.storage();
        let mut batch = Vec::new();
        for index in 0..20 {
            let mut row = record(
                1_700_000_000 + index * 600,
                if index % 4 == 0 { "ERROR" } else { "INFO" },
                "m",
            );
            row.service = if index % 2 == 0 {
                "core".to_string()
            } else {
                "api".to_string()
            };
            row.user_id = index % 3;
            row.correlation_id = format!("corr-{index}");
            batch.push(row);
        }
        storage.insert_batch(&batch).unwrap();

        let mut filters = vec![
            QueryFilter::default(),
            QueryFilter {
                levels: vec!["error".to_string(), "warn".to_string()],
                level: "info".to_string(),
                ..QueryFilter::default()
            },
            QueryFilter {
                level: "error".to_string(),
                ..QueryFilter::default()
            },
            QueryFilter {
                service: "core".to_string(),
                category: "system".to_string(),
                ..QueryFilter::default()
            },
            QueryFilter {
                rule_id: "rule-1".to_string(),
                correlation_id: "corr-3".to_string(),
                ..QueryFilter::default()
            },
            QueryFilter {
                user_id: 2,
                ..QueryFilter::default()
            },
            QueryFilter {
                since: 1_700_000_600,
                until: 1_700_005_000,
                ..QueryFilter::default()
            },
            QueryFilter {
                search: "corr-1".to_string(),
                ..QueryFilter::default()
            },
        ];
        filters.push(QueryFilter {
            levels: vec!["error".to_string(), "critical".to_string()],
            level: "warn".to_string(),
            service: "core".to_string(),
            category: "system".to_string(),
            rule_id: "rule-1".to_string(),
            correlation_id: "corr-4".to_string(),
            user_id: 1,
            since: 1_000,
            until: 9_999_999_999,
            search: "m".to_string(),
            trace_id: String::new(),
            request_id: String::new(),
            fingerprint: String::new(),
        });

        for filter in filters {
            let label = filter.describe();
            storage
                .query(&filter, &Page::clamped(25, 5, false))
                .unwrap_or_else(|err| panic!("query [{label}]: {err:?}"));
            storage
                .query(
                    &filter,
                    &Page::keyset_page(5, true, Some(Keyset::new(1_700_005_000, 3))),
                )
                .unwrap_or_else(|err| panic!("query/keyset [{label}]: {err:?}"));
            storage
                .count(&filter, Some(5))
                .unwrap_or_else(|err| panic!("count [{label}]: {err:?}"));
            storage
                .count(&filter, None)
                .unwrap_or_else(|err| panic!("count uncapped [{label}]: {err:?}"));
            let stats = storage
                .stats(&StatsScope {
                    rule_id: filter.rule_id.clone(),
                    user_id: filter.user_id,
                })
                .unwrap_or_else(|err| panic!("stats [{label}]: {err:?}"));
            assert!(stats.total >= 0);
            storage
                .top_groups(&filter, GroupBy::Service, 10)
                .unwrap_or_else(|err| panic!("top_groups [{label}]: {err:?}"));
            let buckets = storage
                .time_buckets(&filter, 600, 64)
                .unwrap_or_else(|err| panic!("time_buckets [{label}]: {err:?}"));
            let bucketed: i64 = buckets.iter().map(|b| b.count).sum();
            assert_eq!(
                bucketed,
                storage.count(&filter, None).unwrap(),
                "buckets must cover every match [{label}]"
            );
            storage
                .error_aggregates(&filter, 10)
                .unwrap_or_else(|err| panic!("error_aggregates [{label}]: {err:?}"));
            storage
                .export_chunk(&filter, &Page::keyset_page(5, false, None))
                .unwrap_or_else(|err| panic!("export_chunk [{label}]: {err:?}"));
        }

        // The two statements that carry the protected-category guard, the exclusion
        // set and the cutoff all run without touching a row here.
        let report = storage
            .purge(
                &PurgeFilter {
                    level: "debug".to_string(),
                    older_than_hours: 1,
                    protect_categories: vec!["audit".to_string()],
                    now: 1_700_000_000,
                    ..PurgeFilter::default()
                },
                3,
                10,
            )
            .unwrap();
        assert_eq!(report.purged, 0);
        assert_eq!(report.remaining, 20);
        let sweep = query::RetentionSweep {
            filter: QueryFilter {
                level: "trace".to_string(),
                ..QueryFilter::default()
            },
            cutoff: Some(-1),
            protected: vec!["audit".to_string(), "security".to_string()],
            exclusions: query::OverrideSet {
                levels: vec!["debug".to_string()],
                categories: vec!["security".to_string()],
                services: vec!["api".to_string()],
            },
        };
        let report = storage.retention_purge(&sweep, 4, 20).unwrap();
        assert_eq!(report.purged, 0);
        assert_eq!(report.remaining, 20);
    }

    #[test]
    fn group_listings_break_ties_on_the_first_row_not_on_the_planner() {
        // `core` and `api` hold six rows each in this fixture. Ordering by volume
        // alone would leave the tie to the query planner — SQLite answers
        // `GROUP BY service` through `idx_logs_service`, i.e. alphabetically — so
        // the listing has an explicit, plan-independent tie-break: the group whose
        // first row entered the archive first wins.
        let archive = TempArchive::new();
        let (storage, _metrics) = archive.storage();
        let mut batch = Vec::new();
        for index in 0..12 {
            let mut row = record(
                1_700_000_100 + index,
                if index % 3 == 0 { "ERROR" } else { "INFO" },
                "m",
            );
            row.service = if index % 2 == 0 {
                "core".to_string()
            } else {
                "api".to_string()
            };
            row.rule_id = if index < 6 {
                "rule-a".to_string()
            } else {
                String::new()
            };
            row.user_id = if index < 4 { 1 } else { 2 };
            batch.push(row);
        }
        storage.insert_batch(&batch).unwrap();

        let expected = vec![("core".to_string(), 6), ("api".to_string(), 6)];
        for round in 0..3 {
            let top = storage
                .top_groups(&QueryFilter::default(), GroupBy::Service, 10)
                .unwrap();
            assert_eq!(top, expected, "round {round}: the tie order must be stable");
        }
        // A filtered listing keeps the same rule and the same counts. Tenant 1 owns
        // the first four rows — two `core`, two `api` — so this is a tie again and
        // `core` still wins it.
        let top = storage
            .top_groups(
                &QueryFilter {
                    user_id: 1,
                    ..QueryFilter::default()
                },
                GroupBy::Service,
                10,
            )
            .unwrap();
        assert_eq!(
            top,
            vec![("core".to_string(), 2), ("api".to_string(), 2)],
            "only the first four rows belong to tenant 1"
        );
        // `by_rule` is whole-archive, exactly like the Python engine, and it is not
        // truncated by the scoped slice.
        let stats = storage
            .stats(&StatsScope {
                rule_id: String::new(),
                user_id: 1,
            })
            .unwrap();
        assert_eq!(stats.total, 4);
        assert_eq!(stats.by_rule.get("rule-a"), Some(&6));
    }

    #[test]
    fn disk_full_and_io_write_failures_are_transient_not_backend() {
        // Classification decides whether a producer keeps its record, so pin
        // the codes, not just the enum name. `SqliteFailure` carries an
        // extended code in `ErrorCode::Extended`; the primary code is implied.
        fn sqlite_failure(extended: i32, message: &str) -> rusqlite::Error {
            rusqlite::Error::SqliteFailure(
                rusqlite::ffi::Error::new(extended),
                Some(message.to_string()),
            )
        }
        for code in [SQLITE_FULL, SQLITE_IOERR_WRITE, SQLITE_IOERR_FSYNC] {
            let err = sqlite_failure(code, "database or disk is full");
            let mapped = map_error(err, "insert batch");
            assert!(mapped.is_retryable(), "code {code} must be retryable");
            let rendered = format!("{mapped}");
            assert!(
                rendered.contains("disk-full"),
                "code {code} must name the condition for the runbook: {rendered}"
            );
        }
        // A genuinely broken statement stays in the Backend bucket.
        let err = rusqlite::Error::InvalidQuery;
        assert!(!map_error(err, "prepare read").is_retryable());
        // ... and a Busy lock is still Busy, unchanged behaviour.
        let err = sqlite_failure(SQLITE_BUSY, "database is locked");
        assert!(matches!(map_error(err, "x"), StorageError::Busy(_)));
    }

    #[test]
    fn wal_size_reflects_the_journal_file_and_zero_without_one() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("waltest.db").to_string_lossy().to_string();
        let cfg = Config::default();
        let backend = BackendConfig {
            backend: Backend::Sqlite,
            sqlite_path: path,
            postgres_dsn: None,
            busy_timeout_ms: 5_000,
            read_pool_size: 1,
            pool_timeout_ms: 5_000,
            statement_timeout_ms: 0,
            transaction_timeout_ms: 0,
            max_rows_per_statement: 500,
        };
        let metrics = Arc::new(Metrics::new());
        let storage = SqliteStorage::open(&cfg, &backend, Arc::clone(&metrics)).unwrap();
        // Fresh open with no writes yet: the journal may exist but is empty.
        assert!(storage.wal_size_bytes() >= 0);

        // A committed batch must materialise WAL bytes while the connection is
        // open (the storage never checkpoints away an in-flight journal before
        // close). Not exact-size: WAL growth is SQLite's business. The gauge's
        // contract is "unreported = 0", "reported = what fs says".
        let record = Record {
            id: 0,
            ts: 1_700_000_000,
            level: "INFO".into(),
            service: "core".into(),
            category: "system".into(),
            message: "wal gauge".into(),
            detail: String::new(),
            rule_id: String::new(),
            user_id: 0,
            correlation_id: String::new(),
            request_id: String::new(),
            trace_id: String::new(),
            module: String::new(),
            fingerprint: String::new(),
        };
        storage.insert_batch(&[record]).unwrap();
        assert!(
            storage.wal_size_bytes() > 0,
            "a commit must leave journal bytes on disk, fs said {}",
            storage.wal_size_bytes()
        );

        // The absent file case: a never-opened path reports 0 (via map_or), never
        // a failure.
        assert_eq!(
            std::fs::metadata(format!(
                "{}-wal",
                dir.path().join("none.db").to_string_lossy()
            ))
            .map(|m| m.len() as i64)
            .unwrap_or(0),
            0
        );
    }
}
