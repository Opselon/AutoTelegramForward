//! PostgreSQL storage: the second first-class backend.
//!
//! docs/logging-contract.md §4 requires a bounded pool with acquisition, statement
//! and transaction timeouts, batched inserts, bounded ingestion parallelism,
//! saturation observability, and *retries that do not duplicate records*. Each of
//! those maps to something concrete here:
//!
//! * **Bounded pool.** [`ClientPool`] holds at most `read_pool_size` connections;
//!   acquisition waits on a condvar for at most `pool_timeout_ms` and then fails
//!   with `STORAGE_BUSY` — counted as `atf_logger_storage_pool_timeouts_total`.
//!   Waiting time is a histogram, so saturation is visible before it is fatal.
//! * **Statement/transaction timeouts.** Every pooled connection is configured with
//!   `statement_timeout` and `idle_in_transaction_session_timeout`, so a stuck
//!   statement cannot hold a connection (or a row lock) forever. They are the
//!   server-side guard; the client-side guard is the pool's acquisition timeout.
//! * **Batched inserts.** One multi-row `INSERT` per statement, wrapped in one
//!   transaction, chunked at `max_rows_per_statement` (1000 rows × 9 columns stays
//!   well under PostgreSQL's 65535-parameter ceiling).
//! * **Retries that do not duplicate.** Retrying is only ever attempted for a
//!   *transaction* that failed, never for a batch that was already committed — see
//!   `insert_batch_with_retry` in `storage::mod`: it re-runs the whole batch only
//!   after a retryable error, and a retryable error means no commit happened
//!   (serialization failure, deadlock, lock timeout, connection loss). At-least-once
//!   is the contract; this is implemented so a retry cannot double-write a batch
//!   that succeeded.
//! * **No silent fallback.** `open` fails loudly when PostgreSQL is unreachable or
//!   its schema cannot be migrated, and nothing anywhere in this crate substitutes a
//!   different backend.
//!
//! NOT IMPLEMENTED, stated plainly: TLS. The `postgres` crate is built with
//! `default-features = false` (no TLS feature), so connections to a
//! TLS-requiring server fail instead of silently downgrading. Use a socket, a
//! private network, or terminate TLS in a proxy. This is documented in README.md.
//!
//! Integration tests run against a real server only when one is reachable
//! (`ATF_LOGGER_TEST_PG_DSN`, or `ATF_TEST_PG_DSN`); otherwise they print that they
//! were skipped rather than pretending to pass. See `tests/` and the README.

use super::{Backend, BackendConfig, PurgeReport, Record, Storage, StorageError};
use crate::config::Config;
use crate::metrics::{Histogram, Metrics};
use crate::query::{
    self, Bucket, ErrorAgg, GroupBy, Page, PurgeFilter, QueryFilter, RetentionSweep, SqlDialect,
    Stats, StatsScope,
};
use postgres::types::ToSql;
use postgres::{Client, NoTls};
use std::str::FromStr;
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

/// The PostgreSQL schema. Column names and semantics mirror the SQLite archive
/// exactly, so a row moved between backends keeps its meaning.
pub const POSTGRES_MIGRATIONS: &[&str] = &[
    "\
CREATE TABLE IF NOT EXISTS logs (
    id BIGSERIAL PRIMARY KEY,
    ts BIGINT NOT NULL,
    level TEXT NOT NULL,
    service TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    rule_id TEXT NOT NULL DEFAULT '',
    user_id BIGINT NOT NULL DEFAULT 0,
    correlation_id TEXT NOT NULL DEFAULT ''
)",
    // Added by migration rather than a rebuild: instant on a large table, and old
    // rows carry the documented defaults.
    "ALTER TABLE logs ADD COLUMN IF NOT EXISTS rule_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE logs ADD COLUMN IF NOT EXISTS user_id BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE logs ADD COLUMN IF NOT EXISTS correlation_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE logs ADD COLUMN IF NOT EXISTS request_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE logs ADD COLUMN IF NOT EXISTS trace_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE logs ADD COLUMN IF NOT EXISTS module TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE logs ADD COLUMN IF NOT EXISTS fingerprint TEXT NOT NULL DEFAULT ''",
    // Same index set and the same names as the SQLite archive, justified by the
    // predicates `query::where_clause` builds: equality on level/service/category/
    // rule_id/user_id/correlation_id/trace_id/fingerprint and range on ts.
    "CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)",
    "CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)",
    "CREATE INDEX IF NOT EXISTS idx_logs_category ON logs(category)",
    "CREATE INDEX IF NOT EXISTS idx_logs_service ON logs(service)",
    "CREATE INDEX IF NOT EXISTS idx_logs_rule ON logs(rule_id)",
    "CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_logs_corr ON logs(correlation_id)",
    "CREATE INDEX IF NOT EXISTS idx_logs_trace ON logs(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_logs_fingerprint ON logs(fingerprint)",
];

/// SQLSTATEs worth retrying: the transaction did not commit, so re-running it
/// cannot duplicate a record.
///
/// `40001` serialization_failure, `40P01` deadlock_detected, `55P03`
/// lock_not_available, `53300` too_many_connections, `57P01/57P02/57P03`
/// shutdown/crash/cannot_connect_now, `08000/08003/08006` connection exceptions.
pub fn is_retryable_sqlstate(code: &str) -> bool {
    matches!(
        code,
        "40001"
            | "40P01"
            | "55P03"
            | "53300"
            | "57P01"
            | "57P02"
            | "57P03"
            | "08000"
            | "08003"
            | "08006"
    )
}

fn map_pg_error(err: postgres::Error, context: &str) -> StorageError {
    if let Some(db_error) = err.as_db_error() {
        let state = db_error.code().code();
        let message = format!("{}: {} ({})", context, db_error.message(), state);
        if is_retryable_sqlstate(state) {
            return StorageError::Transient(message);
        }
        return StorageError::Backend(message);
    }
    if err.is_closed() {
        return StorageError::Transient(format!("{context}: connection closed"));
    }
    StorageError::Backend(format!("{context}: {err}"))
}

/// The bounded connection pool.
pub struct ClientPool {
    dsn: String,
    capacity: usize,
    acquisition_timeout: Duration,
    connect_timeout: Duration,
    statement_timeout_ms: u64,
    transaction_timeout_ms: u64,
    state: Mutex<PoolState>,
    ready: Condvar,
    metrics: Arc<Metrics>,
}

#[derive(Default)]
struct PoolState {
    idle: Vec<Client>,
    /// Connections created and not yet discarded; the only thing that bounds the
    /// pool's memory.
    created: usize,
}

impl ClientPool {
    fn new(backend: &BackendConfig, metrics: Arc<Metrics>) -> Result<Arc<Self>, StorageError> {
        let dsn = backend
            .postgres_dsn
            .clone()
            .ok_or_else(|| StorageError::Config("PostgreSQL DSN is missing".to_string()))?;
        let pool = Arc::new(ClientPool {
            dsn,
            capacity: backend.read_pool_size.clamp(1, 64),
            acquisition_timeout: Duration::from_millis(backend.pool_timeout_ms.max(1)),
            connect_timeout: Duration::from_millis(backend.pool_timeout_ms.clamp(100, 10_000)),
            statement_timeout_ms: backend.statement_timeout_ms,
            transaction_timeout_ms: backend.transaction_timeout_ms,
            state: Mutex::new(PoolState::default()),
            ready: Condvar::new(),
            metrics: Arc::clone(&metrics),
        });
        metrics.storage_pool_capacity.set(pool.capacity as i64);
        // Fail loudly now, not on the first write: a Logger that cannot reach its
        // database must not start pretending it can.
        let probe = pool.connect()?;
        probe
            .close()
            .map_err(|err| map_pg_error(err, "close probe"))?;
        Ok(pool)
    }

    fn connect(&self) -> Result<Client, StorageError> {
        let mut config = postgres::Config::from_str(&self.dsn)
            .map_err(|err| StorageError::Config(format!("invalid PostgreSQL DSN: {err}")))?;
        config.connect_timeout(self.connect_timeout);
        let mut client = config
            .connect(NoTls)
            .map_err(|err| map_pg_error(err, "connect"))?;
        // Server-side guardrails. The values are parsed integers, so interpolating
        // them cannot inject anything, and PostgreSQL does not accept placeholders in
        // SET.
        let mut statements = String::new();
        if self.statement_timeout_ms > 0 {
            statements.push_str(&format!(
                "SET statement_timeout = {};\n",
                self.statement_timeout_ms
            ));
        }
        if self.transaction_timeout_ms > 0 {
            statements.push_str(&format!(
                "SET idle_in_transaction_session_timeout = {};\n",
                self.transaction_timeout_ms
            ));
        }
        statements.push_str("SET application_name = 'atf-logger';\n");
        client
            .batch_execute(&statements)
            .map_err(|err| map_pg_error(err, "configure session"))?;
        Ok(client)
    }

    /// Take a connection, waiting at most `pool_timeout_ms`.
    pub fn acquire(&self) -> Result<PooledClient<'_>, StorageError> {
        let deadline = Instant::now() + self.acquisition_timeout;
        let started = Instant::now();
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        loop {
            if let Some(client) = state.idle.pop() {
                self.metrics.storage_pool_in_use.inc();
                self.metrics
                    .storage_pool_wait_seconds
                    .observe_seconds(started.elapsed().as_secs_f64());
                return Ok(PooledClient {
                    pool: self,
                    client: Some(client),
                    broken: false,
                });
            }
            if state.created < self.capacity {
                state.created += 1;
                drop(state);
                match self.connect() {
                    Ok(client) => {
                        self.metrics.storage_pool_in_use.inc();
                        self.metrics
                            .storage_pool_wait_seconds
                            .observe_seconds(started.elapsed().as_secs_f64());
                        return Ok(PooledClient {
                            pool: self,
                            client: Some(client),
                            broken: false,
                        });
                    }
                    Err(err) => {
                        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
                        state.created = state.created.saturating_sub(1);
                        self.ready.notify_one();
                        return Err(err);
                    }
                }
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                self.metrics.storage_pool_timeouts.inc();
                self.metrics
                    .storage_pool_wait_seconds
                    .observe_seconds(started.elapsed().as_secs_f64());
                return Err(StorageError::Busy(format!(
                    "no PostgreSQL connection available within {} ms (pool={})",
                    self.acquisition_timeout.as_millis(),
                    self.capacity
                )));
            }
            let (guard, timeout) = self
                .ready
                .wait_timeout(state, remaining)
                .unwrap_or_else(|e| e.into_inner());
            state = guard;
            if timeout.timed_out() && state.idle.is_empty() && state.created >= self.capacity {
                self.metrics.storage_pool_timeouts.inc();
                return Err(StorageError::Busy(format!(
                    "no PostgreSQL connection available within {} ms (pool={})",
                    self.acquisition_timeout.as_millis(),
                    self.capacity
                )));
            }
        }
    }

    fn release(&self, client: Client) {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.idle.push(client);
        self.metrics.storage_pool_in_use.dec();
        self.ready.notify_one();
    }

    fn discard(&self, _client: Client) {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.created = state.created.saturating_sub(1);
        self.metrics.storage_pool_in_use.dec();
        self.ready.notify_one();
    }

    fn idle_count(&self) -> usize {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .idle
            .len()
    }
}

/// A leased connection; returns itself to the pool on drop unless it was marked
/// broken (a closed/broken connection is discarded, not recycled into the pool).
pub struct PooledClient<'a> {
    pool: &'a ClientPool,
    client: Option<Client>,
    broken: bool,
}

impl PooledClient<'_> {
    pub fn client(&mut self) -> &mut Client {
        self.client.as_mut().expect("pooled client is present")
    }

    /// Mark the connection unusable: it will be dropped instead of returned.
    pub fn mark_broken(&mut self) {
        self.broken = true;
    }
}

impl Drop for PooledClient<'_> {
    fn drop(&mut self) {
        if let Some(client) = self.client.take() {
            if self.broken {
                self.pool.discard(client);
            } else {
                self.pool.release(client);
            }
        }
    }
}

/// PostgreSQL-backed storage.
pub struct PgStorage {
    pool: Arc<ClientPool>,
    metrics: Arc<Metrics>,
    target: String,
    max_rows_per_statement: usize,
}

impl PgStorage {
    /// Run `body` inside one transaction on a pooled client.
    ///
    /// The connection is marked broken — and therefore discarded instead of being
    /// recycled — whenever the transaction did not complete: a session that failed
    /// mid-transaction is not a session to hand to the next writer.
    fn with_transaction<T>(
        &self,
        context: &str,
        body: impl FnOnce(&mut postgres::Transaction<'_>) -> Result<T, StorageError>,
    ) -> Result<T, StorageError> {
        let mut pooled = self.pool.acquire()?;
        let mut broken = false;
        let outcome = {
            let client = pooled.client();
            match client.transaction() {
                Err(err) => {
                    broken = true;
                    Err(map_pg_error(err, context))
                }
                Ok(mut transaction) => match body(&mut transaction) {
                    Ok(value) => match transaction.commit() {
                        Ok(()) => Ok(value),
                        Err(err) => {
                            broken = true;
                            Err(map_pg_error(err, context))
                        }
                    },
                    Err(err) => {
                        broken = true;
                        let _ = transaction.rollback();
                        Err(err)
                    }
                },
            }
        };
        if broken {
            pooled.mark_broken();
        }
        outcome
    }

    pub fn open(
        _cfg: &Config,
        backend: &BackendConfig,
        metrics: Arc<Metrics>,
    ) -> Result<Self, StorageError> {
        let pool = ClientPool::new(backend, Arc::clone(&metrics))?;
        let storage = PgStorage {
            pool,
            metrics,
            target: backend.describe_safe(),
            max_rows_per_statement: backend.max_rows_per_statement.clamp(1, 1000),
        };
        // Migrate on the same connection path the writer uses, so a missing CREATE
        // privilege fails startup instead of failing an ingest later.
        storage.migrate()?;
        storage.probe()?;
        Ok(storage)
    }

    /// Apply the schema. Idempotent; safe on an existing database.
    pub fn migrate(&self) -> Result<(), StorageError> {
        let mut pooled = self.pool.acquire()?;
        let client = pooled.client();
        let mut transaction = client
            .transaction()
            .map_err(|err| map_pg_error(err, "begin migration"))?;
        for statement in POSTGRES_MIGRATIONS {
            if let Err(err) = transaction.batch_execute(statement) {
                return Err(map_pg_error(err, "migration"));
            }
        }
        transaction
            .commit()
            .map_err(|err| map_pg_error(err, "commit migration"))?;
        Ok(())
    }

    fn timed<T>(
        &self,
        histogram: &Histogram,
        operation: impl FnOnce() -> Result<T, StorageError>,
    ) -> Result<T, StorageError> {
        let started = Instant::now();
        let result = operation();
        histogram.observe_seconds(started.elapsed().as_secs_f64());
        result
    }

    /// Bind a plan's parameters for `postgres`.
    fn bind(params: &[query::Param]) -> Vec<&(dyn ToSql + Sync)> {
        params
            .iter()
            .map(|param| match param {
                query::Param::Int(value) => value as &(dyn ToSql + Sync),
                query::Param::Text(value) => value as &(dyn ToSql + Sync),
            })
            .collect()
    }

    fn read_rows<T>(
        &self,
        plan: &query::Plan,
        map: impl Fn(&postgres::Row) -> Result<T, StorageError>,
    ) -> Result<Vec<T>, StorageError> {
        let mut pooled = self.pool.acquire()?;
        let params = Self::bind(&plan.params);
        let rows = match pooled.client().query(&plan.sql, &params) {
            Ok(rows) => rows,
            Err(err) => {
                // A failed read discards the connection: `map_pg_error` cannot tell a
                // SQL-level failure from a dead socket, and reconnecting costs less
                // than handing a suspect session to the next reader.
                pooled.mark_broken();
                return Err(map_pg_error(err, "query"));
            }
        };
        rows.iter().map(map).collect()
    }

    fn read_one_i64(&self, plan: &query::Plan) -> Result<i64, StorageError> {
        let mut pooled = self.pool.acquire()?;
        let params = Self::bind(&plan.params);
        let row = match pooled.client().query_one(&plan.sql, &params) {
            Ok(row) => row,
            Err(err) => {
                pooled.mark_broken();
                return Err(map_pg_error(err, "query scalar"));
            }
        };
        Ok(row.get::<_, Option<i64>>(0).unwrap_or(0))
    }

    fn read_pairs(&self, plan: &query::Plan) -> Result<Vec<(String, i64)>, StorageError> {
        self.read_rows(plan, |row| {
            Ok((
                row.get::<_, Option<String>>(0).unwrap_or_default(),
                row.get::<_, i64>(1),
            ))
        })
    }

    /// Number of pooled connections currently idle, for diagnostics.
    pub fn idle_connections(&self) -> usize {
        self.pool.idle_count()
    }
}

fn row_to_record(row: &postgres::Row) -> Result<Record, StorageError> {
    Ok(Record {
        id: row.get::<_, i64>("id"),
        ts: row.get::<_, i64>("ts"),
        level: row.get::<_, Option<String>>("level").unwrap_or_default(),
        service: row.get::<_, Option<String>>("service").unwrap_or_default(),
        category: row.get::<_, Option<String>>("category").unwrap_or_default(),
        message: row.get::<_, Option<String>>("message").unwrap_or_default(),
        detail: row.get::<_, Option<String>>("detail").unwrap_or_default(),
        rule_id: row.get::<_, Option<String>>("rule_id").unwrap_or_default(),
        user_id: row.get::<_, Option<i64>>("user_id").unwrap_or(0),
        correlation_id: row
            .get::<_, Option<String>>("correlation_id")
            .unwrap_or_default(),
        request_id: row
            .get::<_, Option<String>>("request_id")
            .unwrap_or_default(),
        trace_id: row.get::<_, Option<String>>("trace_id").unwrap_or_default(),
        module: row.get::<_, Option<String>>("module").unwrap_or_default(),
        fingerprint: row
            .get::<_, Option<String>>("fingerprint")
            .unwrap_or_default(),
    })
}

/// `INSERT INTO logs (...) VALUES ($1, ..., $13), ($14, ...)` for `rows` rows.
pub fn insert_sql(rows: usize, dialect_start: usize) -> String {
    let mut sql = String::from(
        "INSERT INTO logs (ts, level, service, category, message, detail, rule_id, user_id, \
         correlation_id, request_id, trace_id, module, fingerprint) VALUES ",
    );
    let mut parameter = dialect_start;
    for row in 0..rows {
        if row > 0 {
            sql.push_str(", ");
        }
        sql.push('(');
        for column in 0..13 {
            if column > 0 {
                sql.push_str(", ");
            }
            sql.push_str(&format!("${parameter}"));
            parameter += 1;
        }
        sql.push(')');
    }
    sql
}

impl Storage for PgStorage {
    fn backend(&self) -> Backend {
        Backend::Postgres
    }

    fn insert_batch(&self, records: &[Record]) -> Result<usize, StorageError> {
        if records.is_empty() {
            return Ok(0);
        }
        let started = Instant::now();
        let chunk_rows = self.max_rows_per_statement;
        let mut written = 0usize;
        // Chunked so the parameter count stays far below PostgreSQL's 65535 limit
        // (chunk × 13 placeholders), with one transaction for the whole batch: a
        // batch either commits entirely or not at all.
        self.with_transaction("insert batch", |transaction| {
            for chunk in records.chunks(chunk_rows) {
                let sql = insert_sql(chunk.len(), 1);
                let mut owned: Vec<Box<dyn ToSql + Sync>> = Vec::with_capacity(chunk.len() * 13);
                for record in chunk {
                    owned.push(Box::new(record.ts));
                    owned.push(Box::new(record.level.clone()));
                    owned.push(Box::new(record.service.clone()));
                    owned.push(Box::new(record.category.clone()));
                    owned.push(Box::new(record.message.clone()));
                    owned.push(Box::new(record.detail.clone()));
                    owned.push(Box::new(record.rule_id.clone()));
                    owned.push(Box::new(record.user_id));
                    owned.push(Box::new(record.correlation_id.clone()));
                    owned.push(Box::new(record.request_id.clone()));
                    owned.push(Box::new(record.trace_id.clone()));
                    owned.push(Box::new(record.module.clone()));
                    owned.push(Box::new(record.fingerprint.clone()));
                }
                let params: Vec<&(dyn ToSql + Sync)> = owned
                    .iter()
                    .map(|value| value.as_ref() as &(dyn ToSql + Sync))
                    .collect();
                let affected = transaction
                    .execute(&sql, &params)
                    .map_err(|err| map_pg_error(err, "insert batch"))?;
                written += affected as usize;
            }
            Ok(())
        })?;
        self.metrics
            .storage_write_seconds
            .observe_seconds(started.elapsed().as_secs_f64());
        Ok(written)
    }

    fn query(&self, filter: &QueryFilter, page: &Page) -> Result<(Vec<Record>, i64), StorageError> {
        let select = query::select_page(filter, page, SqlDialect::Postgres);
        let rows = self.timed(&self.metrics.storage_read_seconds, || {
            self.read_rows(&select, row_to_record)
        })?;
        // Same fast path as SQLite: a first page that is not full is the whole match
        // set, so no COUNT is issued at all.
        if page.offset == 0 && rows.len() < page.limit {
            let seen = rows.len() as i64;
            return Ok((rows, seen));
        }
        let count_plan = query::count(filter, None, SqlDialect::Postgres);
        let total = self.timed(&self.metrics.storage_read_seconds, || {
            self.read_one_i64(&count_plan)
        })?;
        Ok((rows, total))
    }

    fn count(&self, filter: &QueryFilter, cap: Option<i64>) -> Result<i64, StorageError> {
        let plan = query::count(filter, cap, SqlDialect::Postgres);
        self.timed(&self.metrics.storage_read_seconds, || {
            self.read_one_i64(&plan)
        })
    }

    fn table_rows(&self) -> Result<i64, StorageError> {
        let plan = query::count(&QueryFilter::default(), None, SqlDialect::Postgres);
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
        let aggregate = query::stats(scope, SqlDialect::Postgres);
        let mut pooled = self.pool.acquire()?;
        let params = Self::bind(&aggregate.params);
        let row = pooled
            .client()
            .query_one(&aggregate.sql, &params)
            .map_err(|err| map_pg_error(err, "read stats"))?;
        drop(pooled);
        let mut stats = Stats {
            total: row.get::<_, Option<i64>>(0).unwrap_or(0),
            oldest_ts: row.get::<_, Option<i64>>(1).unwrap_or(0),
            newest_ts: row.get::<_, Option<i64>>(2).unwrap_or(0),
            ..Stats::default()
        };
        for (by, target) in [
            (GroupBy::Level, 0u8),
            (GroupBy::Category, 1u8),
            (GroupBy::Service, 2u8),
        ] {
            let plan = query::group_by(&scoped, by, query::MAX_GROUPS, SqlDialect::Postgres);
            for (key, value) in self.timed(&self.metrics.storage_read_seconds, || {
                self.read_pairs(&plan)
            })? {
                match target {
                    0 => {
                        stats.by_level.insert(key, value);
                    }
                    1 => {
                        stats.by_category.insert(key, value);
                    }
                    _ => {
                        stats.by_service.insert(key, value);
                    }
                }
            }
        }
        // Whole-archive by_rule, exactly like the Python engine and the SQLite path.
        let by_rule = query::by_rule(query::MAX_RULE_GROUPS, SqlDialect::Postgres);
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
        let plan = query::group_by(
            filter,
            by,
            limit.min(query::MAX_GROUPS),
            SqlDialect::Postgres,
        );
        self.timed(&self.metrics.storage_read_seconds, || {
            self.read_pairs(&plan)
        })
    }

    fn error_aggregates(
        &self,
        filter: &QueryFilter,
        limit: usize,
    ) -> Result<Vec<ErrorAgg>, StorageError> {
        let plan = query::error_aggregates(filter, limit, SqlDialect::Postgres);
        self.timed(&self.metrics.storage_read_seconds, || {
            self.read_rows(&plan, |row| {
                Ok(ErrorAgg {
                    fingerprint: row.get::<_, Option<String>>(0).unwrap_or_default(),
                    level: row.get::<_, Option<String>>(1).unwrap_or_default(),
                    service: row.get::<_, Option<String>>(2).unwrap_or_default(),
                    category: row.get::<_, Option<String>>(3).unwrap_or_default(),
                    module: row.get::<_, Option<String>>(4).unwrap_or_default(),
                    message: row.get::<_, Option<String>>(5).unwrap_or_default(),
                    count: row.get::<_, i64>(6),
                    first_ts: row.get::<_, Option<i64>>(7).unwrap_or(0),
                    last_ts: row.get::<_, Option<i64>>(8).unwrap_or(0),
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
        let plan = query::time_buckets(filter, bucket_secs, max_buckets, SqlDialect::Postgres);
        self.timed(&self.metrics.storage_read_seconds, || {
            self.read_rows(&plan, |row| {
                Ok(Bucket {
                    start_ts: row.get::<_, Option<i64>>(0).unwrap_or(0),
                    count: row.get::<_, i64>(1),
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
        let mut protected: Vec<String> = filter
            .protect_categories
            .iter()
            .map(|c| c.trim().to_uppercase())
            .filter(|c| !c.is_empty())
            .collect();
        protected.sort();
        protected.dedup();

        let mut purged: i64 = 0;
        let mut exhausted = false;
        self.with_transaction("purge", |transaction| {
            loop {
                let remaining_budget = max_rows.saturating_sub(purged as u64);
                if remaining_budget == 0 {
                    exhausted = true;
                    break;
                }
                let take = chunk.min(remaining_budget as usize).max(1);
                let plan =
                    query::delete_chunk(&scoped, cutoff, &protected, take, SqlDialect::Postgres);
                let params = Self::bind(&plan.params);
                let deleted = transaction
                    .execute(&plan.sql, &params)
                    .map_err(|err| map_pg_error(err, "purge chunk"))?;
                purged += deleted as i64;
                if deleted == 0 {
                    break;
                }
            }
            Ok(())
        })?;

        let mut protected_skipped = 0i64;
        if !protected.is_empty() {
            let plan = query::count_protected_candidates(
                &scoped,
                cutoff,
                &protected,
                1_000_000,
                SqlDialect::Postgres,
            );
            protected_skipped = self.read_one_i64(&plan).unwrap_or(0);
            self.metrics
                .retention_protected_rows_skipped
                .add(protected_skipped.max(0) as u64);
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
            protected_skipped: protected_skipped > 0,
        })
    }

    fn export_chunk(&self, filter: &QueryFilter, page: &Page) -> Result<Vec<Record>, StorageError> {
        let plan = query::select_page(filter, page, SqlDialect::Postgres);
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
        let mut protected: Vec<String> = sweep
            .protected
            .iter()
            .map(|c| c.trim().to_uppercase())
            .filter(|c| !c.is_empty())
            .collect();
        protected.sort();
        protected.dedup();
        let effective = RetentionSweep {
            protected: protected.clone(),
            ..sweep.clone()
        };

        let mut purged: i64 = 0;
        let mut exhausted = false;
        self.with_transaction("retention sweep", |transaction| {
            loop {
                let remaining_budget = max_rows.saturating_sub(purged as u64);
                if remaining_budget == 0 {
                    exhausted = true;
                    break;
                }
                let take = chunk.min(remaining_budget as usize).max(1);
                let plan = query::retention_delete_chunk(&effective, take, SqlDialect::Postgres);
                let params = Self::bind(&plan.params);
                let deleted = transaction
                    .execute(&plan.sql, &params)
                    .map_err(|err| map_pg_error(err, "retention chunk"))?;
                purged += deleted as i64;
                if deleted == 0 {
                    break;
                }
            }
            Ok(())
        })?;

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
            let mut pooled = self.pool.acquire()?;
            // Touching `logs` proves the schema exists and the connection works.
            pooled
                .client()
                .query("SELECT 1 FROM logs LIMIT 1", &[])
                .map_err(|err| map_pg_error(err, "readiness probe"))?;
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
        // Deliberately a no-op. PostgreSQL reclaims dead tuples through autovacuum;
        // running VACUUM from the ingest path would fight it and take locks for no
        // benefit. Nothing here weakens the ack contract: a committed INSERT is
        // durable regardless of when the space is reclaimed.
        Ok(())
    }

    fn describe(&self) -> String {
        format!("postgres {} idle={}", self.target, self.idle_connections())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn insert_sql_numbers_parameters_from_one() {
        let sql = insert_sql(2, 1);
        assert!(sql.starts_with("INSERT INTO logs (ts, level, service, category, message, detail, rule_id, user_id, correlation_id, request_id, trace_id, module, fingerprint) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13), ($14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26)"), "{sql}");
        assert_eq!(insert_sql(1, 5).matches('$').count(), 13);
        assert!(insert_sql(1, 1).contains("$1"));
    }

    #[test]
    fn every_read_plan_binds_exactly_the_parameters_it_names() {
        // The adapter is `client.query(&plan.sql, &bind(&plan.params))`. Once the
        // numbering and the vector drift apart PostgreSQL answers
        // `Wrong number of parameters passed to query` at runtime, which is the
        // failure this crate shipped with: the plan named a parameter it never
        // bound. Numbering must start at `$1`, be contiguous, and follow the order
        // the values are pushed.
        let filter = QueryFilter {
            levels: vec!["error".to_string(), "warn".to_string()],
            service: "core".to_string(),
            category: "system".to_string(),
            rule_id: "rule-1".to_string(),
            correlation_id: "corr-1".to_string(),
            user_id: 42,
            since: 1_700_000_000,
            until: 1_700_000_999,
            search: "needle".to_string(),
            level: String::new(),
            trace_id: String::new(),
            request_id: String::new(),
            fingerprint: String::new(),
        };
        let scope = StatsScope {
            rule_id: "rule-1".to_string(),
            user_id: 42,
        };
        let sweep = RetentionSweep {
            filter: filter.clone(),
            cutoff: Some(1_700_000_000),
            protected: vec!["audit".to_string()],
            exclusions: query::OverrideSet {
                levels: vec!["debug".to_string()],
                categories: vec!["security".to_string()],
                services: vec!["api".to_string()],
            },
        };
        let plans = [
            (
                "select_page",
                query::select_page(&filter, &Page::clamped(25, 50, false), SqlDialect::Postgres),
            ),
            (
                "select_page/keyset",
                query::select_page(
                    &filter,
                    &Page::keyset_page(25, false, Some(query::Keyset::new(1_000, 7))),
                    SqlDialect::Postgres,
                ),
            ),
            (
                "count",
                query::count(&filter, Some(500), SqlDialect::Postgres),
            ),
            ("stats", query::stats(&scope, SqlDialect::Postgres)),
            (
                "group_by",
                query::group_by(&filter, GroupBy::Rule, 10, SqlDialect::Postgres),
            ),
            (
                "by_rule",
                query::by_rule(query::MAX_RULE_GROUPS, SqlDialect::Postgres),
            ),
            (
                "time_buckets",
                query::time_buckets(&filter, 3_600, 24, SqlDialect::Postgres),
            ),
            (
                "error_aggregates",
                query::error_aggregates(&filter, 20, SqlDialect::Postgres),
            ),
            (
                "delete_chunk",
                query::delete_chunk(
                    &filter,
                    Some(1_000),
                    &["audit".to_string()],
                    500,
                    SqlDialect::Postgres,
                ),
            ),
            (
                "retention_delete_chunk",
                query::retention_delete_chunk(&sweep, 500, SqlDialect::Postgres),
            ),
        ];
        for (what, plan) in plans {
            assert_eq!(
                PgStorage::bind(&plan.params).len(),
                plan.params.len(),
                "{what}: bind() must return one value per parameter"
            );
            assert_eq!(
                placeholder_order(&plan.sql),
                (1..=plan.params.len()).collect::<Vec<_>>(),
                "{what}: $n is not contiguous in binding order: {}",
                plan.sql
            );
        }

        // The insert statement is built with its own counter and has to line up too.
        for rows in [1usize, 2, 9, 17] {
            let sql = insert_sql(rows, 1);
            assert_eq!(
                placeholder_order(&sql),
                (1..=rows * 13).collect::<Vec<_>>(),
                "insert of {rows} rows: {sql}"
            );
        }
    }

    /// Every `$n` the statement uses, in the order the text first uses it. A
    /// repeated `$n` counts once — that is the whole point of a named parameter.
    fn placeholder_order(sql: &str) -> Vec<usize> {
        let chars: Vec<char> = sql.chars().collect();
        let mut numbers: Vec<usize> = Vec::new();
        let mut index = 0;
        while index < chars.len() {
            if chars[index] == '$' {
                let mut end = index + 1;
                while end < chars.len() && chars[end].is_ascii_digit() {
                    end += 1;
                }
                if end > index + 1 {
                    let digits: String = chars[index + 1..end].iter().collect();
                    let number: usize = digits.parse().expect("placeholder number");
                    if !numbers.contains(&number) {
                        numbers.push(number);
                    }
                    index = end;
                    continue;
                }
            }
            index += 1;
        }
        numbers
    }

    /// Live round trip against a real server, for every read path this adapter
    /// builds. Skipped — loudly — when no DSN is configured, so the suite stays
    /// green on a machine without PostgreSQL; an explicit skip, never a silent
    /// pass. Set `ATF_LOGGER_TEST_PG_DSN` to a throwaway server to run it.
    #[test]
    fn live_round_trip_when_a_server_is_reachable() {
        let Some(dsn) = std::env::var("ATF_LOGGER_TEST_PG_DSN")
            .ok()
            .filter(|dsn| !dsn.is_empty())
        else {
            eprintln!("SKIPPED (no ATF_LOGGER_TEST_PG_DSN): PostgreSQL round trip not run");
            return;
        };
        let config = Config {
            db_path: dsn,
            ..Default::default()
        };
        let backend = BackendConfig::from_config(&config, &|_: &str| None)
            .expect("a postgres:// DSN selects the PostgreSQL backend");
        let storage = crate::storage::open(&config, &backend, Arc::new(Metrics::new()))
            .expect("open + migrate");
        assert_eq!(storage.backend(), Backend::Postgres);
        storage.probe().expect("probe");
        storage
            .purge(&PurgeFilter::default(), 1_000, u64::MAX)
            .expect("clear leftovers");
        assert_eq!(storage.table_rows().unwrap(), 0);

        let tag = format!("live-{}", std::process::id());
        let rows: Vec<Record> = (0..12)
            .map(|i: i64| Record {
                id: 0,
                ts: 1_700_000_100 + i * 60,
                level: if i % 3 == 0 { "ERROR" } else { "INFO" }.to_string(),
                service: if i % 2 == 0 { "core" } else { "api" }.to_string(),
                category: "system".to_string(),
                message: format!("{tag} m"),
                detail: "{}".to_string(),
                rule_id: if i < 6 { "rule-a" } else { "" }.to_string(),
                user_id: if i < 4 { 1 } else { 2 },
                correlation_id: format!("{tag}-{i}"),
                request_id: String::new(),
                trace_id: String::new(),
                module: String::new(),
                fingerprint: String::new(),
            })
            .collect();
        assert_eq!(storage.insert_batch(&rows).expect("batched insert"), 12);

        let searched = QueryFilter {
            search: tag.clone(),
            ..QueryFilter::default()
        };
        let (page, total) = storage
            .query(&searched, &Page::clamped(25, 0, false))
            .expect("query with limit and offset");
        assert_eq!(total, 12);
        assert_eq!(
            page.len(),
            12,
            "a bounded page still returns every match here"
        );
        assert!(page.windows(2).all(|pair| pair[0].ts >= pair[1].ts));
        assert_eq!(
            storage
                .query(
                    &searched,
                    &Page::keyset_page(5, false, Some(query::Keyset::new(1_700_000_700, 0)))
                )
                .expect("keyset page")
                .0
                .len(),
            5
        );
        assert_eq!(storage.count(&searched, None).unwrap(), 12);
        assert_eq!(storage.count(&searched, Some(3)).unwrap(), 3);

        let stats = storage.stats(&StatsScope::default()).expect("stats");
        assert_eq!(stats.total, 12);
        assert_eq!(stats.by_level.get("ERROR"), Some(&4));
        assert_eq!(stats.by_service.get("core"), Some(&6));
        assert_eq!(stats.by_rule.get("rule-a"), Some(&6));
        assert_eq!(stats.oldest_ts, 1_700_000_100);
        assert_eq!(stats.newest_ts, 1_700_000_760);

        // The bucket width is named once and used twice in the SELECT list, i.e.
        // *before* the filter's placeholders in the text — the live check that the
        // numbering matches the binding order.
        let buckets = storage
            .time_buckets(&searched, 60, 64)
            .expect("time buckets");
        let bucketed: i64 = buckets.iter().map(|bucket| bucket.count).sum();
        assert_eq!(bucketed, 12, "{buckets:?}");
        assert_eq!(
            buckets.first().map(|b| b.start_ts),
            Some(1_700_000_100 / 60 * 60)
        );

        let groups = storage
            .top_groups(&QueryFilter::default(), GroupBy::Service, 10)
            .expect("top groups");
        assert_eq!(
            groups,
            vec![("core".to_string(), 6), ("api".to_string(), 6)],
            "the tie is broken by the group's first row, on this backend too"
        );
        let failures = storage
            .error_aggregates(&QueryFilter::default(), 10)
            .expect("error aggregates");
        assert_eq!(failures.len(), 1, "{failures:?}");
        assert_eq!(failures[0].count, 4);
        assert_eq!(
            storage
                .export_chunk(&searched, &Page::keyset_page(4, false, None))
                .expect("export chunk")
                .len(),
            4
        );

        // A sweep that matches nothing must not touch the archive.
        let sweep = RetentionSweep {
            filter: QueryFilter {
                levels: vec!["TRACE".to_string()],
                ..QueryFilter::default()
            },
            cutoff: Some(i64::MAX),
            protected: vec!["audit".to_string()],
            exclusions: query::OverrideSet::default(),
        };
        let report = storage.retention_purge(&sweep, 500, 1_000).expect("sweep");
        assert_eq!(report.purged, 0);
        assert_eq!(report.remaining, 12);
        assert_eq!(storage.table_rows().unwrap(), 12);

        storage
            .purge(&PurgeFilter::default(), 1_000, u64::MAX)
            .expect("cleanup");
        assert_eq!(storage.table_rows().unwrap(), 0);
        eprintln!("PostgreSQL live round trip passed");
    }

    #[test]
    fn retryable_sqlstates_are_the_no_commit_ones() {
        assert!(is_retryable_sqlstate("40001")); // serialization_failure
        assert!(is_retryable_sqlstate("40P01")); // deadlock_detected
        assert!(is_retryable_sqlstate("57P03")); // cannot_connect_now
        assert!(is_retryable_sqlstate("08006")); // connection_failure
                                                 // A constraint or syntax error must never be retried: retrying cannot fix it.
        assert!(!is_retryable_sqlstate("23505")); // unique_violation
        assert!(!is_retryable_sqlstate("42601")); // syntax_error
        assert!(!is_retryable_sqlstate("42P01")); // undefined_table
    }

    #[test]
    fn migrations_are_idempotent_statements() {
        // Every statement must be safe to re-run on an existing database.
        for statement in POSTGRES_MIGRATIONS {
            let lowered = statement.to_lowercase();
            assert!(
                lowered.contains("if not exists"),
                "migration is not idempotent: {statement}"
            );
        }
        // The schema mirrors the SQLite column set.
        for column in [
            "ts",
            "level",
            "service",
            "category",
            "message",
            "detail",
            "rule_id",
            "user_id",
            "correlation_id",
        ] {
            assert!(
                POSTGRES_MIGRATIONS[0].contains(column),
                "column {column} missing from the CREATE TABLE"
            );
        }
    }

    #[test]
    fn dsn_without_tls_config_is_rejected_at_construction() {
        let backend = BackendConfig {
            backend: Backend::Postgres,
            sqlite_path: String::new(),
            postgres_dsn: None,
            busy_timeout_ms: 1_000,
            read_pool_size: 2,
            pool_timeout_ms: 500,
            statement_timeout_ms: 1_000,
            transaction_timeout_ms: 1_000,
            max_rows_per_statement: 1000,
        };
        let metrics = Arc::new(Metrics::new());
        let err = match ClientPool::new(&backend, metrics) {
            Ok(_) => panic!("an unreachable server must not produce a pool"),
            Err(err) => err,
        };
        assert_eq!(err.code(), "STORAGE_CONFIG");
    }
}
