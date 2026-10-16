//! Filter model and SQL construction, shared by both storage backends.
//!
//! The filter semantics are the ones the existing Python engine implemented in
//! `LoggerDatabase._filters` / `query` / `stats` / `purge`, because the Go API and
//! the bot panel depend on them:
//!
//! * `levels` (multi-select) wins over `level`; both are upper-cased.
//! * `service`, `category`, `rule_id`, `correlation_id`, `user_id` are exact
//!   matches; `user_id == 0` means "every tenant".
//! * `search` is a substring match over `message` **or** `detail`, case-insensitive
//!   (SQLite's `LIKE` is ASCII-case-insensitive by default, so PostgreSQL uses
//!   `ILIKE` to keep the two backends answer the same question).
//! * `since`/`until` are inclusive `ts` bounds, where `0` means unbounded.
//! * Default order is newest-first (`ts DESC, id DESC`) with a stable tiebreak, so
//!   offset pagination cannot repeat or skip a row with duplicate timestamps.
//!
//! Two things are deliberately *stronger* than the Python engine:
//!
//! * every statement lists its columns explicitly — there is no `SELECT *`, so a
//!   future column cannot silently widen a response or a row size;
//! * counting is bounded (`count_capped`) and offset pagination can be upgraded to
//!   a keyset seek, so a hot path never pays for an unbounded full-table `COUNT`.
//!
//! Index strategy (see `storage::sqlite::migrate`): the existing Python indexes on
//! `ts`, `level`, `category`, `service`, `rule_id`, `user_id`, `correlation_id` are
//! preserved and reused, because those are exactly the equality/range predicates
//! this module builds. A substring `search` cannot use an index in either backend;
//! it is therefore always combined with the indexed predicates and bounded by
//! `LIMIT`, which is the only honest thing `LIKE '%x%'` can promise.

use crate::storage::Record;
use std::collections::BTreeMap;

/// Explicit column list — the single source of truth for read statements.
pub const COLUMNS: &str =
    "id, ts, level, service, category, message, detail, rule_id, user_id, correlation_id";

/// Upper bound on rows returned by one read, matching the Python engine's cap.
pub const MAX_PAGE: usize = 1000;
/// Default page size, matching the Python engine.
pub const DEFAULT_PAGE: usize = 100;
/// How many groups `aggregate`/`stats` maps may contain before they are truncated.
pub const MAX_GROUPS: usize = 1000;
/// The Python engine's `by_rule` cap.
pub const MAX_RULE_GROUPS: usize = 100;
/// Row cap for one unbounded COUNT. Keeps "how many matches are there" from
/// becoming a full-table scan on a multi-gigabyte archive.
pub const DEFAULT_COUNT_CAP: i64 = 1_000_000;

/// Placeholder syntax differs between the two backends.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SqlDialect {
    Sqlite,
    Postgres,
}

/// A bound parameter, in the order it must be bound.
#[derive(Debug, Clone, PartialEq)]
pub enum Param {
    Int(i64),
    Text(String),
}

/// A finished statement with its parameters.
#[derive(Debug, Clone, PartialEq)]
pub struct Plan {
    pub sql: String,
    pub params: Vec<Param>,
}

/// Incrementally builds a statement, allocating placeholders as it goes so a
/// PostgreSQL plan's `$n` numbering can never drift out of order.
#[derive(Debug)]
pub struct Builder {
    dialect: SqlDialect,
    next: usize,
    params: Vec<Param>,
}

impl Builder {
    pub fn new(dialect: SqlDialect) -> Self {
        Builder {
            dialect,
            next: 1,
            params: Vec::new(),
        }
    }

    pub fn bind(&mut self, param: Param) -> String {
        self.params.push(param);
        match self.dialect {
            SqlDialect::Sqlite => "?".to_string(),
            SqlDialect::Postgres => {
                let placeholder = format!("${}", self.next);
                self.next += 1;
                placeholder
            }
        }
    }

    pub fn bind_text(&mut self, value: &str) -> String {
        self.bind(Param::Text(value.to_string()))
    }

    pub fn bind_int(&mut self, value: i64) -> String {
        self.bind(Param::Int(value))
    }

    /// Bind one value the statement needs `occurrences` times.
    ///
    /// The two dialects cannot be treated the same here. PostgreSQL *names* a
    /// parameter, so one value can be bound once and its `$n` repeated in the text.
    /// SQLite has no named parameters in these statements: every `?` is positional
    /// and consumes exactly one bound value, so the value has to be bound once per
    /// occurrence. Reusing a returned `?` string is what produced the runtime
    /// failure `Wrong number of parameters passed to query. Got 2, needed 3` — three
    /// placeholders in the text, two values supplied. Binding twice is free for the
    /// planner and is the only way the placeholder count and the parameter count
    /// stay equal on both backends.
    pub fn bind_repeated(&mut self, param: Param, occurrences: usize) -> Vec<String> {
        let occurrences = occurrences.max(1);
        match self.dialect {
            SqlDialect::Postgres => {
                let placeholder = self.bind(param);
                vec![placeholder; occurrences]
            }
            SqlDialect::Sqlite => (0..occurrences).map(|_| self.bind(param.clone())).collect(),
        }
    }

    /// Which backend this statement is being built for. Needed where the two
    /// dialects disagree on semantics rather than syntax (`LIKE` is case-insensitive
    /// for ASCII in SQLite but case-sensitive in PostgreSQL, so PostgreSQL uses
    /// `ILIKE` to answer the same question).
    pub fn dialect(&self) -> SqlDialect {
        self.dialect
    }

    pub fn params(self) -> Vec<Param> {
        self.params
    }
}

/// The shared filter model.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct QueryFilter {
    /// Multi-select severity filter; when non-empty it wins over `level`.
    pub levels: Vec<String>,
    pub level: String,
    pub service: String,
    pub category: String,
    pub rule_id: String,
    pub correlation_id: String,
    pub user_id: i64,
    pub since: i64,
    pub until: i64,
    /// Substring search over `message` and `detail`.
    pub search: String,
}

impl QueryFilter {
    /// True when the filter matches every row (used to pick the cheap COUNT path
    /// and to refuse accidental blanket work).
    pub fn is_unfiltered(&self) -> bool {
        self.chosen_levels().is_empty()
            && self.level.trim().is_empty()
            && self.service.trim().is_empty()
            && self.category.trim().is_empty()
            && self.rule_id.trim().is_empty()
            && self.correlation_id.trim().is_empty()
            && self.user_id == 0
            && self.since == 0
            && self.until == 0
            && self.search.trim().is_empty()
    }

    /// Upper-cased, non-empty severity selection (Python's `chosen`).
    pub fn chosen_levels(&self) -> Vec<String> {
        self.levels
            .iter()
            .map(|l| l.trim().to_uppercase())
            .filter(|l| !l.is_empty())
            .collect()
    }

    /// A short, bounded description for logs and metrics labels (never the raw
    /// search text: it can contain a secret and would blow up label cardinality).
    pub fn describe(&self) -> String {
        let mut parts = Vec::new();
        let chosen = self.chosen_levels();
        if !chosen.is_empty() {
            parts.push(format!("levels={}", chosen.join("|")));
        } else if !self.level.trim().is_empty() {
            parts.push(format!("level={}", self.level.trim().to_uppercase()));
        }
        if !self.service.trim().is_empty() {
            parts.push(format!("service={}", self.service.trim()));
        }
        if !self.category.trim().is_empty() {
            parts.push(format!("category={}", self.category.trim()));
        }
        if !self.rule_id.trim().is_empty() {
            parts.push("rule_id=*".to_string());
        }
        if !self.correlation_id.trim().is_empty() {
            parts.push("correlation_id=*".to_string());
        }
        if self.user_id != 0 {
            parts.push("user_id=*".to_string());
        }
        if !self.search.trim().is_empty() {
            parts.push(format!("search({} chars)", self.search.chars().count()));
        }
        if self.since != 0 {
            parts.push(format!("since={}", self.since));
        }
        if self.until != 0 {
            parts.push(format!("until={}", self.until));
        }
        if parts.is_empty() {
            "all".to_string()
        } else {
            parts.join(",")
        }
    }
}

/// Where the next page starts. OFF equality/range seek, added for internal export
/// and paging use; the wire contract keeps `offset` because the Go API depends on
/// it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Keyset {
    pub ts: i64,
    pub id: i64,
}

impl Keyset {
    pub fn new(ts: i64, id: i64) -> Self {
        Keyset { ts, id }
    }
    pub fn from_record(record: &Record) -> Self {
        Keyset {
            ts: record.ts,
            id: record.id,
        }
    }
}

/// Page addressing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Page {
    pub limit: usize,
    pub offset: usize,
    pub ascending: bool,
    pub keyset: Option<Keyset>,
}

impl Default for Page {
    fn default() -> Self {
        Page {
            limit: DEFAULT_PAGE,
            offset: 0,
            ascending: false,
            keyset: None,
        }
    }
}

impl Page {
    /// Clamp a wire-provided page into the supported range.
    pub fn clamped(limit: i32, offset: i32, ascending: bool) -> Self {
        let limit = if limit <= 0 {
            DEFAULT_PAGE
        } else {
            (limit as usize).min(MAX_PAGE)
        };
        Page {
            limit,
            offset: offset.max(0) as usize,
            ascending,
            keyset: None,
        }
    }

    pub fn keyset_page(limit: usize, ascending: bool, keyset: Option<Keyset>) -> Self {
        Page {
            limit: limit.clamp(1, MAX_PAGE),
            offset: 0,
            ascending,
            keyset,
        }
    }
}

/// `LogStats` scope.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StatsScope {
    pub rule_id: String,
    pub user_id: i64,
}

/// Aggregate result of `LogStats`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Stats {
    pub total: i64,
    pub by_level: BTreeMap<String, i64>,
    pub by_category: BTreeMap<String, i64>,
    pub by_rule: BTreeMap<String, i64>,
    pub by_service: BTreeMap<String, i64>,
    pub oldest_ts: i64,
    pub newest_ts: i64,
}

/// A purge request.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PurgeFilter {
    pub level: String,
    pub service: String,
    pub category: String,
    pub rule_id: String,
    /// Protect the newest N hours. `0` means no time-based protection.
    pub older_than_hours: u64,
    /// Categories that must survive this purge (audit/security by default).
    pub protect_categories: Vec<String>,
    /// `now` in the unit the `ts` column uses, supplied by the caller.
    pub now: i64,
}

impl PurgeFilter {
    /// The `ts` cutoff implied by `older_than_hours`, or `None` when unbounded.
    pub fn cutoff(&self) -> Option<i64> {
        if self.older_than_hours == 0 {
            None
        } else {
            let hours = self.older_than_hours.min(i64::MAX as u64) as i64;
            Some(self.now.saturating_sub(hours.saturating_mul(3600)))
        }
    }

    pub fn describe(&self) -> String {
        format!(
            "level={} service={} category={} rule_id={} older_than_hours={} protected=[{}]",
            if self.level.is_empty() {
                "-"
            } else {
                &self.level
            },
            if self.service.is_empty() {
                "-"
            } else {
                &self.service
            },
            if self.category.is_empty() {
                "-"
            } else {
                &self.category
            },
            if self.rule_id.is_empty() { "-" } else { "*" },
            self.older_than_hours,
            self.protect_categories.join(",")
        )
    }
}

/// Result of a purge.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PurgeOutcome {
    pub purged: i64,
    /// Rows left in the table (whole table, exactly like the Python engine's
    /// `remaining`, so a client's "there is still history" check keeps working).
    pub remaining: i64,
}

/// Grouping selector for `top_groups`/`aggregate`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GroupBy {
    Level,
    Service,
    Category,
    Rule,
}

impl GroupBy {
    pub fn column(self) -> &'static str {
        match self {
            GroupBy::Level => "level",
            GroupBy::Service => "service",
            GroupBy::Category => "category",
            GroupBy::Rule => "rule_id",
        }
    }
}

/// One time bucket: `bucket_start_ts` (aligned to the bucket width) and its count.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Bucket {
    pub start_ts: i64,
    pub count: i64,
}

/// Recurring-error aggregate: the message signature plus its count and window.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ErrorAgg {
    pub level: String,
    pub category: String,
    pub message: String,
    pub count: i64,
    pub last_ts: i64,
}

/// Build the shared `WHERE` clause.
///
/// `keyset` adds the seek predicate; `extra_not_in` adds the protected-category
/// exclusion used by retention.
pub fn where_clause(
    filter: &QueryFilter,
    keyset: Option<Keyset>,
    ascending: bool,
    extra_not_in: &[String],
    builder: &mut Builder,
) -> String {
    let mut sql = String::from(" WHERE 1=1");
    let chosen = filter.chosen_levels();
    if !chosen.is_empty() {
        let placeholders: Vec<String> = chosen
            .iter()
            .map(|level| builder.bind_text(level))
            .collect();
        sql.push_str(&format!(" AND level IN ({})", placeholders.join(", ")));
    } else if !filter.level.trim().is_empty() {
        let placeholder = builder.bind_text(&filter.level.trim().to_uppercase());
        sql.push_str(&format!(" AND level={placeholder}"));
    }
    if !filter.service.trim().is_empty() {
        let placeholder = builder.bind_text(filter.service.trim());
        sql.push_str(&format!(" AND service={placeholder}"));
    }
    if !filter.category.trim().is_empty() {
        let placeholder = builder.bind_text(filter.category.trim());
        sql.push_str(&format!(" AND category={placeholder}"));
    }
    if !filter.rule_id.trim().is_empty() {
        let placeholder = builder.bind_text(filter.rule_id.trim());
        sql.push_str(&format!(" AND rule_id={placeholder}"));
    }
    if !filter.correlation_id.trim().is_empty() {
        let placeholder = builder.bind_text(filter.correlation_id.trim());
        sql.push_str(&format!(" AND correlation_id={placeholder}"));
    }
    if filter.user_id != 0 {
        let placeholder = builder.bind_int(filter.user_id);
        sql.push_str(&format!(" AND user_id={placeholder}"));
    }
    if !filter.search.trim().is_empty() {
        let like = format!("%{}%", filter.search);
        // PostgreSQL's LIKE is case-sensitive; SQLite's is not (ASCII). ILIKE is what
        // makes the same search string return the same rows on both backends.
        let operator = if builder.dialect() == SqlDialect::Postgres {
            "ILIKE"
        } else {
            "LIKE"
        };
        let first = builder.bind_text(&like);
        let second = builder.bind_text(&like);
        sql.push_str(&format!(
            " AND (message {operator} {first} OR detail {operator} {second})"
        ));
    }
    if filter.since != 0 {
        let placeholder = builder.bind_int(filter.since);
        sql.push_str(&format!(" AND ts >= {placeholder}"));
    }
    if filter.until != 0 {
        let placeholder = builder.bind_int(filter.until);
        sql.push_str(&format!(" AND ts <= {placeholder}"));
    }
    if !extra_not_in.is_empty() {
        let placeholders: Vec<String> = extra_not_in
            .iter()
            .map(|c| builder.bind_text(&c.to_uppercase()))
            .collect();
        sql.push_str(&format!(
            " AND UPPER(category) NOT IN ({})",
            placeholders.join(", ")
        ));
    }
    if let Some(keyset) = keyset {
        let (op, tie) = if ascending { (">", ">") } else { ("<", "<") };
        let ts_ph = builder.bind_int(keyset.ts);
        let ts_ph2 = builder.bind_int(keyset.ts);
        let id_ph = builder.bind_int(keyset.id);
        sql.push_str(&format!(
            " AND (ts {op} {ts_ph} OR (ts = {ts_ph2} AND id {tie} {id_ph}))"
        ));
    }
    sql
}

/// Order clause: a stable tiebreak on `id` is what makes offset paging correct
/// when several records share a timestamp.
pub fn order_clause(ascending: bool) -> &'static str {
    if ascending {
        " ORDER BY ts ASC, id ASC"
    } else {
        " ORDER BY ts DESC, id DESC"
    }
}

/// Bounded count of the rows a purge would have deleted if their category were not
/// protected. Used to report *why* a purge deleted less than an operator expected,
/// which is the difference between "retention is broken" and "retention spared
/// your audit trail".
pub fn count_protected_candidates(
    filter: &QueryFilter,
    cutoff: Option<i64>,
    categories: &[String],
    cap: i64,
    dialect: SqlDialect,
) -> Plan {
    let mut builder = Builder::new(dialect);
    let where_sql = where_clause(filter, None, false, &[], &mut builder);
    let placeholders: Vec<String> = categories
        .iter()
        .map(|c| builder.bind_text(&c.to_uppercase()))
        .collect();
    let mut sql = format!("SELECT COUNT(*) AS total FROM (SELECT 1 AS one FROM logs{where_sql}");
    sql.push_str(&format!(
        " AND UPPER(category) IN ({})",
        placeholders.join(", ")
    ));
    if let Some(cutoff) = cutoff {
        let placeholder = builder.bind_int(cutoff);
        sql.push_str(&format!(" AND ts < {placeholder}"));
    }
    let cap_ph = builder.bind_int(cap.max(1));
    sql.push_str(&format!(" LIMIT {cap_ph}) AS bounded"));
    Plan {
        sql,
        params: builder.params(),
    }
}

/// `SELECT` for one page of records.
pub fn select_page(filter: &QueryFilter, page: &Page, dialect: SqlDialect) -> Plan {
    let mut builder = Builder::new(dialect);
    let mut sql = format!("SELECT {COLUMNS} FROM logs");
    sql.push_str(&where_clause(
        filter,
        page.keyset,
        page.ascending,
        &[],
        &mut builder,
    ));
    sql.push_str(order_clause(page.ascending));
    let limit_ph = builder.bind_int(page.limit as i64);
    sql.push_str(&format!(" LIMIT {limit_ph}"));
    // The Python engine's read statement is always `... LIMIT ? OFFSET ?`; mirroring
    // it keeps the two engines' statements and parameter lists identical for the same
    // wire request (an OFFSET of 0 is a no-op). A keyset seek addresses the page by
    // `(ts, id)` and therefore replaces the offset entirely — only that path omits
    // the clause.
    if page.keyset.is_none() {
        let offset_ph = builder.bind_int(page.offset as i64);
        sql.push_str(&format!(" OFFSET {offset_ph}"));
    }
    Plan {
        sql,
        params: builder.params(),
    }
}

/// Exact (but optionally capped) match count for a filter.
pub fn count(filter: &QueryFilter, cap: Option<i64>, dialect: SqlDialect) -> Plan {
    let mut builder = Builder::new(dialect);
    let where_sql = where_clause(filter, None, false, &[], &mut builder);
    let sql = match cap {
        Some(cap) => {
            let cap_ph = builder.bind_int(cap.max(1));
            format!("SELECT COUNT(*) AS total FROM (SELECT 1 AS one FROM logs{where_sql} LIMIT {cap_ph}) AS bounded")
        }
        None => format!("SELECT COUNT(*) AS total FROM logs{where_sql}"),
    };
    Plan {
        sql,
        params: builder.params(),
    }
}

/// `Chosen severity counts + history window for `LogStats`.
pub fn stats(scope: &StatsScope, dialect: SqlDialect) -> Plan {
    let filter = QueryFilter {
        rule_id: scope.rule_id.clone(),
        user_id: scope.user_id,
        ..QueryFilter::default()
    };
    let mut builder = Builder::new(dialect);
    let where_sql = where_clause(&filter, None, false, &[], &mut builder);
    Plan {
        sql: format!(
            "SELECT COUNT(*) AS total, MIN(ts) AS oldest_ts, MAX(ts) AS newest_ts FROM logs{where_sql}"
        ),
        params: builder.params(),
    }
}

/// `GROUP BY` statement for one dimension.
///
/// Ordering is `count DESC` — the "top" of a top-N listing is the biggest group —
/// with a *deterministic* tie-break on the group's first row (`MIN(id)`, i.e.
/// insertion order). Without it, two groups of equal size come back in whatever
/// order the planner produced: SQLite answers `GROUP BY service` in ascending key
/// order through `idx_logs_service`, so a tie silently depended on an index. The
/// tie-break is explicit, identical on both backends, and stable across a restart.
pub fn group_by(filter: &QueryFilter, by: GroupBy, limit: usize, dialect: SqlDialect) -> Plan {
    let mut builder = Builder::new(dialect);
    let where_sql = where_clause(filter, None, false, &[], &mut builder);
    let column = by.column();
    let limit_ph = builder.bind_int(limit.max(1) as i64);
    Plan {
        sql: format!(
            "SELECT {column} AS k, COUNT(*) AS c FROM logs{where_sql} GROUP BY {column} ORDER BY c DESC, MIN(id) ASC LIMIT {limit_ph}"
        ),
        params: builder.params(),
    }
}

/// The whole-archive `by_rule` aggregate the Python engine ran regardless of scope.
/// Ordered like [`group_by`]: biggest group first, ties broken by the group's first
/// row so the `MAX_RULE_GROUPS` cut is reproducible.
pub fn by_rule(limit: usize, dialect: SqlDialect) -> Plan {
    let mut builder = Builder::new(dialect);
    let limit_ph = builder.bind_int(limit.max(1) as i64);
    Plan {
        sql: format!(
            "SELECT rule_id AS k, COUNT(*) AS c FROM logs WHERE rule_id != '' GROUP BY rule_id ORDER BY c DESC, MIN(id) ASC LIMIT {limit_ph}"
        ),
        params: builder.params(),
    }
}

/// Time-bucketed counts. `bucket_secs` is validated by the caller.
pub fn time_buckets(
    filter: &QueryFilter,
    bucket_secs: i64,
    max_buckets: usize,
    dialect: SqlDialect,
) -> Plan {
    let bucket = bucket_secs.max(1);
    let mut builder = Builder::new(dialect);
    // The bucket width sits in the SELECT list, i.e. it appears *before* the WHERE
    // clause in the statement text. On SQLite `?` is positional, so the value must be
    // bound in text order: binding the filter first handed the level names to the
    // division and the bucket width to `level IN (...)`, which then matched nothing —
    // the statement still *ran*, it just answered an empty result. Bind the SELECT
    // list first, then build the filter into the same placeholders' tail.
    let bucket_placeholders = builder.bind_repeated(Param::Int(bucket), 2);
    let where_sql = where_clause(filter, None, false, &[], &mut builder);
    let limit_ph = builder.bind_int(max_buckets.max(1) as i64);
    // Integer division floors toward zero; `ts` is unsigned-in-practice for real
    // log timestamps, and the explicit `* bucket` keeps the alignment obvious.
    Plan {
        sql: format!(
            "SELECT (ts / {}) * {} AS bucket, COUNT(*) AS c FROM logs{where_sql} GROUP BY bucket ORDER BY bucket ASC LIMIT {limit_ph}",
            bucket_placeholders[0], bucket_placeholders[1]
        ),
        params: builder.params(),
    }
}

/// Recurring-error aggregate: identical messages collapsed, newest first.
///
/// The group key is the message *signature* (`category`, `message`); severity is
/// an attribute of the pattern, not part of its identity. Adding `level` to the
/// group key split a single recurring failure into one row per severity — 15
/// `ERROR` + 15 `WARN` occurrences of the same message reported as two groups,
/// each with half the real count. The reported `level` is the level of the
/// group's newest occurrence, i.e. the row `last_ts` points at, so the caller
/// still sees the severity the pattern currently fires at.
pub fn error_aggregates(filter: &QueryFilter, limit: usize, dialect: SqlDialect) -> Plan {
    let mut scoped = filter.clone();
    // Errors only: the caller may pass a narrower level set, but the aggregate is
    // about failures, so the failure levels are always included.
    if scoped.chosen_levels().is_empty() && scoped.level.trim().is_empty() {
        scoped.levels = vec![
            "WARN".to_string(),
            "ERROR".to_string(),
            "CRITICAL".to_string(),
        ];
    }
    let mut builder = Builder::new(dialect);
    let where_sql = where_clause(&scoped, None, false, &[], &mut builder);
    let limit_ph = builder.bind_int(limit.max(1) as i64);
    Plan {
        sql: format!(
            "SELECT level, category, message, c, last_ts FROM (\
             SELECT level, category, message, \
             COUNT(*) OVER (PARTITION BY category, message) AS c, \
             MAX(ts) OVER (PARTITION BY category, message) AS last_ts, \
             ROW_NUMBER() OVER (PARTITION BY category, message ORDER BY ts DESC, id DESC) AS rn \
             FROM logs{where_sql}) AS pattern WHERE rn = 1 \
             ORDER BY c DESC, last_ts DESC LIMIT {limit_ph}"
        ),
        params: builder.params(),
    }
}

/// `DELETE` statement for one purge chunk.
///
/// `protected` categories are excluded by the same statement that chooses which
/// rows to delete (the guard sits inside the `SELECT` that feeds the `IN` list), so
/// there is no window in which a protected row could be removed. Parameters are
/// emitted in SQL order: filter, protected categories, cutoff, chunk size.
pub fn delete_chunk(
    filter: &QueryFilter,
    cutoff: Option<i64>,
    protected: &[String],
    chunk: usize,
    dialect: SqlDialect,
) -> Plan {
    let mut builder = Builder::new(dialect);
    let where_sql = where_clause(filter, None, false, protected, &mut builder);
    let mut sql = String::from("DELETE FROM logs WHERE id IN (SELECT id FROM logs");
    sql.push_str(&where_sql);
    if let Some(cutoff) = cutoff {
        let placeholder = builder.bind_int(cutoff);
        sql.push_str(&format!(" AND ts < {placeholder}"));
    }
    let limit_ph = builder.bind_int(chunk.max(1) as i64);
    sql.push_str(&format!(" ORDER BY id LIMIT {limit_ph})"));
    Plan {
        sql,
        params: builder.params(),
    }
}

/// `DELETE` every match (used only for an explicitly-scoped operator purge).
pub fn delete_all(
    filter: &QueryFilter,
    cutoff: Option<i64>,
    protected: &[String],
    dialect: SqlDialect,
) -> Plan {
    let mut builder = Builder::new(dialect);
    let where_sql = where_clause(filter, None, false, protected, &mut builder);
    let mut sql = format!("DELETE FROM logs{where_sql}");
    if let Some(cutoff) = cutoff {
        let placeholder = builder.bind_int(cutoff);
        sql.push_str(&format!(" AND ts < {placeholder}"));
    }
    Plan {
        sql,
        params: builder.params(),
    }
}

/// The override selectors that govern some subset of rows.
///
/// Retention applies "first match wins" in the fixed order **level → category →
/// service → default**; each sweep excludes the rows an earlier selector already
/// owns, so exactly one age can ever apply to one row and the union of the sweeps
/// is the whole table. Nothing falls through a gap and no row is judged twice.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct OverrideSet {
    pub levels: Vec<String>,
    pub categories: Vec<String>,
    pub services: Vec<String>,
}

impl OverrideSet {
    pub fn is_empty(&self) -> bool {
        self.levels.is_empty() && self.categories.is_empty() && self.services.is_empty()
    }

    /// ` AND NOT (level IN (...) OR UPPER(category) IN (...) OR service IN (...))`.
    ///
    /// Empty dimensions are skipped so an override set with only level rules does
    /// not silently exclude every row of the other two dimensions.
    pub fn exclusion_sql(&self, builder: &mut Builder) -> String {
        let mut clauses = Vec::new();
        if !self.levels.is_empty() {
            let placeholders: Vec<String> = self
                .levels
                .iter()
                .map(|v| builder.bind_text(&v.to_uppercase()))
                .collect();
            clauses.push(format!("level IN ({})", placeholders.join(", ")));
        }
        if !self.categories.is_empty() {
            let placeholders: Vec<String> = self
                .categories
                .iter()
                .map(|v| builder.bind_text(&v.to_uppercase()))
                .collect();
            clauses.push(format!("UPPER(category) IN ({})", placeholders.join(", ")));
        }
        if !self.services.is_empty() {
            let placeholders: Vec<String> =
                self.services.iter().map(|v| builder.bind_text(v)).collect();
            clauses.push(format!("service IN ({})", placeholders.join(", ")));
        }
        if clauses.is_empty() {
            String::new()
        } else {
            format!(" AND NOT ({})", clauses.join(" OR "))
        }
    }
}

/// One retention sweep: a filter, its cutoff, the protected categories and the
/// overrides an earlier sweep already owns.
///
/// The protected-category guard lives *inside* the `SELECT` that feeds the `DELETE`
/// (never in a read-then-delete pair), so a protected row cannot be observed as
/// deletable and then deleted.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RetentionSweep {
    pub filter: QueryFilter,
    pub cutoff: Option<i64>,
    pub protected: Vec<String>,
    pub exclusions: OverrideSet,
}

/// `DELETE` for one bounded retention chunk. Parameters in SQL order: filter,
/// protected categories, exclusions, cutoff, chunk size.
pub fn retention_delete_chunk(sweep: &RetentionSweep, chunk: usize, dialect: SqlDialect) -> Plan {
    let mut builder = Builder::new(dialect);
    let where_sql = where_clause(&sweep.filter, None, false, &sweep.protected, &mut builder);
    let mut sql = String::from("DELETE FROM logs WHERE id IN (SELECT id FROM logs");
    sql.push_str(&where_sql);
    sql.push_str(&sweep.exclusions.exclusion_sql(&mut builder));
    if let Some(cutoff) = sweep.cutoff {
        let placeholder = builder.bind_int(cutoff);
        sql.push_str(&format!(" AND ts < {placeholder}"));
    }
    let limit_ph = builder.bind_int(chunk.max(1) as i64);
    sql.push_str(&format!(" ORDER BY id LIMIT {limit_ph})"));
    Plan {
        sql,
        params: builder.params(),
    }
}

/// Bounded count of the rows a retention sweep would delete if it were not
/// budget-limited — the number that turns "retention seems slow" into "there are
/// 400k rows due for expiry".
pub fn retention_count_plan(sweep: &RetentionSweep, cap: i64, dialect: SqlDialect) -> Plan {
    let mut builder = Builder::new(dialect);
    let where_sql = where_clause(&sweep.filter, None, false, &sweep.protected, &mut builder);
    let mut sql = format!("SELECT COUNT(*) AS total FROM (SELECT 1 AS one FROM logs{where_sql}");
    sql.push_str(&sweep.exclusions.exclusion_sql(&mut builder));
    if let Some(cutoff) = sweep.cutoff {
        let placeholder = builder.bind_int(cutoff);
        sql.push_str(&format!(" AND ts < {placeholder}"));
    }
    let cap_ph = builder.bind_int(cap.max(1));
    sql.push_str(&format!(" LIMIT {cap_ph}) AS bounded"));
    Plan {
        sql,
        params: builder.params(),
    }
}

/// Bounded-memory export: walks the match set in chunks with a hard row cap.
///
/// The engine never materialises "all matching rows"; this is the only supported
/// way to hand a large result to an exporter (backup, migration to PostgreSQL).
#[derive(Debug, Clone)]
pub struct Exporter {
    filter: QueryFilter,
    chunk: usize,
    max_rows: u64,
    ascending: bool,
    cursor: Option<Keyset>,
    emitted: u64,
    finished: bool,
}

impl Exporter {
    pub fn new(filter: QueryFilter, chunk: usize, max_rows: u64, ascending: bool) -> Self {
        Exporter {
            filter,
            chunk: chunk.clamp(1, MAX_PAGE),
            max_rows,
            ascending,
            cursor: None,
            emitted: 0,
            finished: false,
        }
    }

    pub fn filter(&self) -> &QueryFilter {
        &self.filter
    }

    pub fn emitted(&self) -> u64 {
        self.emitted
    }

    pub fn is_finished(&self) -> bool {
        self.finished
    }

    /// The page to request next, or `None` when the cap/end is reached.
    pub fn next_page(&mut self) -> Option<Page> {
        if self.finished {
            return None;
        }
        let remaining = self.max_rows.saturating_sub(self.emitted);
        if remaining == 0 {
            self.finished = true;
            return None;
        }
        let limit = (self.chunk as u64).min(remaining) as usize;
        Some(Page::keyset_page(limit, self.ascending, self.cursor))
    }

    /// Feed a fetched chunk back in; returns `false` when the walk is complete.
    pub fn accept(&mut self, rows: &[Record]) -> bool {
        if rows.is_empty() {
            self.finished = true;
            return false;
        }
        self.emitted += rows.len() as u64;
        if let Some(last) = rows.last() {
            self.cursor = Some(Keyset::from_record(last));
        }
        if (rows.len() as u64) < self.chunk as u64 || self.emitted >= self.max_rows {
            self.finished = true;
            return false;
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn filter() -> QueryFilter {
        QueryFilter {
            levels: vec!["error".to_string(), "warn".to_string()],
            service: "core".to_string(),
            since: 100,
            ..QueryFilter::default()
        }
    }

    fn placeholders(plan: &Plan) -> Vec<String> {
        plan.params
            .iter()
            .map(|p| match p {
                Param::Int(v) => v.to_string(),
                Param::Text(v) => v.clone(),
            })
            .collect()
    }

    #[test]
    fn never_selects_star() {
        let plan = select_page(
            &QueryFilter::default(),
            &Page::default(),
            SqlDialect::Sqlite,
        );
        assert!(!plan.sql.contains("SELECT *"));
        assert!(plan
            .sql
            .starts_with("SELECT id, ts, level, service, category"));
    }

    #[test]
    fn multi_level_wins_over_single_level_and_is_upper_cased() {
        let mut f = filter();
        f.level = "debug".to_string();
        let plan = select_page(&f, &Page::default(), SqlDialect::Postgres);
        assert!(plan.sql.contains("level IN ($1, $2)"), "{}", plan.sql);
        assert_eq!(
            placeholders(&plan)[..2],
            vec!["ERROR".to_string(), "WARN".to_string()]
        );
        // Single-level path upper-cases too, matching the Python engine.
        f.levels.clear();
        let plan = select_page(&f, &Page::default(), SqlDialect::Postgres);
        assert!(plan.sql.contains("level="), "{}", plan.sql);
        assert_eq!(placeholders(&plan)[0], "DEBUG");
    }

    #[test]
    fn postgres_placeholders_are_sequential() {
        let plan = select_page(&filter(), &Page::default(), SqlDialect::Postgres);
        let numbers: Vec<String> = plan
            .params
            .iter()
            .enumerate()
            .map(|(i, _)| format!("${}", i + 1))
            .collect();
        for n in numbers {
            assert!(plan.sql.contains(&n), "{} missing from {}", n, plan.sql);
        }
        assert_eq!(
            plan.params.len(),
            6,
            "levels(2) service(1) since(1) limit(1) offset(1)"
        );
    }

    #[test]
    fn search_uses_like_on_both_columns() {
        let mut f = QueryFilter::default();
        f.search = "needle".to_string();
        let sqlite = select_page(&f, &Page::default(), SqlDialect::Sqlite);
        assert!(sqlite.sql.contains("(message LIKE ? OR detail LIKE ?)"));
        assert_eq!(placeholders(&sqlite)[0], "%needle%");
        let pg = select_page(&f, &Page::default(), SqlDialect::Postgres);
        assert!(pg.sql.contains("(message ILIKE $1 OR detail ILIKE $2)"));
    }

    #[test]
    fn keyset_seek_replaces_offset() {
        let page = Page::keyset_page(50, false, Some(Keyset::new(1000, 42)));
        let plan = select_page(&QueryFilter::default(), &page, SqlDialect::Sqlite);
        assert!(
            plan.sql.contains("AND (ts < ? OR (ts = ? AND id < ?))"),
            "{}",
            plan.sql
        );
        assert!(!plan.sql.contains("OFFSET"));
        let ascending = Page::keyset_page(50, true, Some(Keyset::new(1000, 42)));
        let plan = select_page(&QueryFilter::default(), &ascending, SqlDialect::Sqlite);
        assert!(plan.sql.contains("AND (ts > ? OR (ts = ? AND id > ?))"));
    }

    #[test]
    fn protected_categories_are_excluded_case_insensitively() {
        let mut f = QueryFilter::default();
        f.category = String::new();
        let mut builder = Builder::new(SqlDialect::Sqlite);
        let sql = where_clause(
            &f,
            None,
            false,
            &["audit".to_string(), "security".to_string()],
            &mut builder,
        );
        assert!(sql.contains("UPPER(category) NOT IN (?, ?)"), "{sql}");
        let params = builder.params();
        assert_eq!(params[0], Param::Text("AUDIT".to_string()));
        assert_eq!(params[1], Param::Text("SECURITY".to_string()));
    }

    #[test]
    fn count_is_capped_and_explicit() {
        let plan = count(&QueryFilter::default(), None, SqlDialect::Sqlite);
        assert_eq!(plan.sql, "SELECT COUNT(*) AS total FROM logs WHERE 1=1");
        let capped = count(&QueryFilter::default(), Some(1000), SqlDialect::Postgres);
        assert!(capped
            .sql
            .contains("FROM (SELECT 1 AS one FROM logs WHERE 1=1 LIMIT $1)"));
        assert_eq!(capped.params[0], Param::Int(1000));
    }

    #[test]
    fn pagination_clamps_the_wire_values() {
        let page = Page::clamped(0, -5, false);
        assert_eq!(page.limit, DEFAULT_PAGE);
        assert_eq!(page.offset, 0);
        let page = Page::clamped(100_000, 7, true);
        assert_eq!(page.limit, MAX_PAGE);
        assert_eq!(page.offset, 7);
        assert!(page.ascending);
    }

    #[test]
    fn stats_and_groups_are_scoped_and_ordered() {
        let scope = StatsScope {
            rule_id: "rule-1".to_string(),
            user_id: 5094837833,
        };
        let plan = stats(&scope, SqlDialect::Sqlite);
        assert!(plan.sql.contains("MIN(ts) AS oldest_ts"));
        assert!(plan.sql.contains("rule_id=?"));
        assert!(plan.sql.contains("user_id=?"));
        let grouped = group_by(
            &QueryFilter::default(),
            GroupBy::Service,
            10,
            SqlDialect::Postgres,
        );
        assert!(grouped
            .sql
            .contains("GROUP BY service ORDER BY c DESC, MIN(id) ASC LIMIT $1"));
        let by_rule = by_rule(MAX_RULE_GROUPS, SqlDialect::Sqlite);
        assert!(by_rule.sql.contains("WHERE rule_id != ''"));
    }

    #[test]
    fn delete_chunk_is_bounded_and_delete_all_is_not() {
        let mut f = QueryFilter::default();
        f.level = "debug".to_string();
        let chunk = delete_chunk(&f, Some(1000), &[], 500, SqlDialect::Sqlite);
        assert!(chunk
            .sql
            .starts_with("DELETE FROM logs WHERE id IN (SELECT id FROM logs"));
        assert!(chunk.sql.contains("AND ts < ?"));
        assert!(chunk.sql.contains("ORDER BY id LIMIT ?"));
        assert_eq!(placeholders(&chunk), vec!["DEBUG", "1000", "500"]);
        // Protected categories are excluded inside the same statement, and the
        // parameters stay in SQL order.
        let guarded = delete_chunk(
            &f,
            Some(1000),
            &["audit".to_string()],
            500,
            SqlDialect::Sqlite,
        );
        assert!(
            guarded
                .sql
                .contains("AND UPPER(category) NOT IN (?) AND ts < ? ORDER BY id LIMIT ?"),
            "{}",
            guarded.sql
        );
        assert_eq!(
            placeholders(&guarded),
            vec!["DEBUG", "AUDIT", "1000", "500"]
        );
        let all = delete_all(&f, None, &[], SqlDialect::Sqlite);
        assert_eq!(all.sql, "DELETE FROM logs WHERE 1=1 AND level=?");
    }

    #[test]
    fn time_buckets_align_to_the_bucket_width() {
        let plan = time_buckets(&QueryFilter::default(), 3600, 24, SqlDialect::Postgres);
        assert!(plan.sql.contains("(ts / $1) * $1 AS bucket"));
        assert_eq!(plan.params[0], Param::Int(3600));
        assert_eq!(plan.params[1], Param::Int(24));
    }

    #[test]
    fn error_aggregates_default_to_failure_levels() {
        let plan = error_aggregates(&QueryFilter::default(), 20, SqlDialect::Sqlite);
        assert!(plan.sql.contains("level IN (?, ?, ?)"));
        // Regression: the group key is the message signature. Severity is an
        // attribute of the pattern, so it must not partition the group (grouping on
        // it reported 15 ERROR + 15 WARN of one message as two groups of 15).
        assert!(
            plan.sql.contains("PARTITION BY category, message"),
            "{}",
            plan.sql
        );
        assert!(!plan.sql.contains("GROUP BY level"), "{}", plan.sql);
        assert_plan_is_bindable(&plan, SqlDialect::Sqlite, "error_aggregates");
    }

    #[test]
    fn error_aggregates_collapse_severities_and_bind_every_dialect() {
        let filters = vec![
            ("default", QueryFilter::default()),
            (
                "levels",
                QueryFilter {
                    levels: vec!["ERROR".to_string(), "CRITICAL".to_string()],
                    ..QueryFilter::default()
                },
            ),
            (
                "service-tenant",
                QueryFilter {
                    service: "core".to_string(),
                    user_id: 42,
                    ..QueryFilter::default()
                },
            ),
            (
                "window-search-correlation",
                QueryFilter {
                    since: 1_700_000_000,
                    until: 1_700_000_500,
                    search: "repeated failure".to_string(),
                    correlation_id: "corr-1".to_string(),
                    ..QueryFilter::default()
                },
            ),
        ];
        for dialect in [SqlDialect::Sqlite, SqlDialect::Postgres] {
            for (label, filter) in &filters {
                let plan = error_aggregates(filter, 20, dialect);
                assert!(
                    plan.sql
                        .contains("COUNT(*) OVER (PARTITION BY category, message)"),
                    "{label}: one count per message signature expected: {}",
                    plan.sql
                );
                assert!(
                    plan.sql.contains("WHERE rn = 1"),
                    "{label}: one row per message signature expected: {}",
                    plan.sql
                );
                assert_plan_is_bindable(&plan, dialect, &format!("error_aggregates {label}"));
            }
        }
        // Levels are inlined when the caller names none, then the limit — the same
        // order the placeholders appear in the statement text.
        let sqlite = error_aggregates(&QueryFilter::default(), 20, SqlDialect::Sqlite);
        assert_eq!(
            sqlite.params,
            vec![
                Param::Text("WARN".to_string()),
                Param::Text("ERROR".to_string()),
                Param::Text("CRITICAL".to_string()),
                Param::Int(20),
            ]
        );
        let postgres = error_aggregates(&QueryFilter::default(), 20, SqlDialect::Postgres);
        assert!(postgres.sql.contains("LIMIT $4"), "{}", postgres.sql);
        assert!(
            postgres.sql.contains("level IN ($1, $2, $3)"),
            "{}",
            postgres.sql
        );
    }

    #[test]
    fn exporter_walks_with_a_hard_cap() {
        let mut exporter = Exporter::new(QueryFilter::default(), 100, 250, false);
        let mut pages = 0;
        let mut total = 0;
        while let Some(page) = exporter.next_page() {
            assert!(page.keyset.is_some() || pages == 0);
            let rows: Vec<Record> = (0..page.limit)
                .map(|i| Record {
                    id: (pages * 100 + i) as i64,
                    ts: 1000 - (pages * 100 + i) as i64,
                    ..Record::default()
                })
                .collect();
            let continue_walk = exporter.accept(&rows);
            total += rows.len();
            pages += 1;
            if !continue_walk {
                break;
            }
        }
        assert_eq!(total, 250, "the cap must be respected exactly");
        assert!(exporter.is_finished());
        assert!(exporter.next_page().is_none());
    }

    #[test]
    fn protected_candidate_count_is_bounded() {
        let mut f = QueryFilter::default();
        f.level = "debug".to_string();
        let plan = count_protected_candidates(
            &f,
            Some(500),
            &["audit".to_string(), "security".to_string()],
            250,
            SqlDialect::Postgres,
        );
        assert!(
            plan.sql
                .contains("FROM (SELECT 1 AS one FROM logs WHERE 1=1 AND level=$1 AND UPPER(category) IN ($2, $3) AND ts < $4 LIMIT $5) AS bounded"),
            "{}",
            plan.sql
        );
        assert_eq!(plan.params.len(), 5);
        assert_eq!(plan.params[1], Param::Text("AUDIT".to_string()));
        assert_eq!(plan.params[4], Param::Int(250));
    }

    #[test]
    fn filter_description_never_echoes_search_text() {
        let mut f = QueryFilter::default();
        f.search = "password=hunter2".to_string();
        f.user_id = 5;
        let described = f.describe();
        assert!(!described.contains("hunter2"));
        assert!(described.contains("search(16 chars)"));
        assert_eq!(QueryFilter::default().describe(), "all");
        assert!(QueryFilter::default().is_unfiltered());
    }

    // ----------------------------------------------------------------------- //
    // Placeholder / parameter budget.
    //
    // The failure these pin was a runtime one, not a formatting one: a statement
    // carried three placeholders but only two values (`Wrong number of parameters
    // passed to query. Got 2, needed 3`) because one bound placeholder was pasted
    // into two positions. Both dialects must bind exactly what the text asks for,
    // and PostgreSQL's `$n` must follow the order the values are bound in.
    // ----------------------------------------------------------------------- //

    /// Every `$n` the statement uses, in the order the text first uses it.
    fn postgres_placeholder_order(sql: &str) -> Vec<usize> {
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

    fn assert_plan_is_bindable(plan: &Plan, dialect: SqlDialect, what: &str) {
        match dialect {
            SqlDialect::Postgres => {
                for number in 1..=plan.params.len() {
                    let placeholder = format!("${number}");
                    assert!(
                        plan.sql.contains(&placeholder),
                        "{what}: {placeholder} is bound but never used: {}",
                        plan.sql
                    );
                }
                assert_eq!(
                    postgres_placeholder_order(&plan.sql),
                    (1..=plan.params.len()).collect::<Vec<_>>(),
                    "{what}: $n does not follow the binding order: {}",
                    plan.sql
                );
            }
            SqlDialect::Sqlite => {
                let placeholders = plan.sql.matches('?').count();
                assert_eq!(
                    placeholders,
                    plan.params.len(),
                    "{what}: {placeholders} placeholders but {} bound values: {}",
                    plan.params.len(),
                    plan.sql
                );
            }
        }
    }

    /// One filter per shape the wire contract can produce, including the
    /// combinations that interleave text and integer parameters.
    fn filter_matrix() -> Vec<(&'static str, QueryFilter)> {
        vec![
            ("unfiltered", QueryFilter::default()),
            (
                "levels list",
                QueryFilter {
                    levels: vec![
                        "error".to_string(),
                        "warn".to_string(),
                        "critical".to_string(),
                    ],
                    ..QueryFilter::default()
                },
            ),
            (
                "single level",
                QueryFilter {
                    level: "debug".to_string(),
                    ..QueryFilter::default()
                },
            ),
            (
                "service+category+rule+correlation",
                QueryFilter {
                    service: "core".to_string(),
                    category: "system".to_string(),
                    rule_id: "rule-1".to_string(),
                    correlation_id: "corr-1".to_string(),
                    ..QueryFilter::default()
                },
            ),
            (
                "tenant",
                QueryFilter {
                    user_id: 5_094_837_833,
                    ..QueryFilter::default()
                },
            ),
            (
                "since+until",
                QueryFilter {
                    since: 1_700_000_000,
                    until: 1_700_000_999,
                    ..QueryFilter::default()
                },
            ),
            (
                "search",
                QueryFilter {
                    search: "needle".to_string(),
                    ..QueryFilter::default()
                },
            ),
            (
                "everything at once",
                QueryFilter {
                    levels: vec!["error".to_string(), "critical".to_string()],
                    level: "warn".to_string(),
                    service: "core".to_string(),
                    category: "system".to_string(),
                    rule_id: "rule-1".to_string(),
                    correlation_id: "corr-1".to_string(),
                    user_id: 42,
                    since: 1_700_000_000,
                    until: 1_700_000_999,
                    search: "needle".to_string(),
                },
            ),
        ]
    }

    #[test]
    fn every_statement_is_bindable_for_every_filter_combination() {
        let sweep = |filter: &QueryFilter| RetentionSweep {
            filter: filter.clone(),
            cutoff: Some(1_700_000_000),
            protected: vec!["audit".to_string()],
            exclusions: OverrideSet {
                levels: vec!["debug".to_string()],
                categories: vec!["security".to_string()],
                services: vec!["api".to_string()],
            },
        };
        for (name, filter) in filter_matrix() {
            for dialect in [SqlDialect::Sqlite, SqlDialect::Postgres] {
                let offset_page = Page::clamped(25, 50, false);
                let keyset_page = Page::keyset_page(25, true, Some(Keyset::new(1000, 7)));
                let first_export_page = Page::keyset_page(50, false, None);
                let scope = StatsScope {
                    rule_id: filter.rule_id.clone(),
                    user_id: filter.user_id,
                };
                let sweeps = sweep(&filter);
                let statements: Vec<(&str, Plan)> = vec![
                    ("select_page", select_page(&filter, &offset_page, dialect)),
                    (
                        "select_page/keyset",
                        select_page(&filter, &keyset_page, dialect),
                    ),
                    (
                        "select_page/first export page",
                        select_page(&filter, &first_export_page, dialect),
                    ),
                    ("count", count(&filter, None, dialect)),
                    ("count capped", count(&filter, Some(500), dialect)),
                    ("stats", stats(&scope, dialect)),
                    (
                        "group_by",
                        group_by(&filter, GroupBy::Category, 10, dialect),
                    ),
                    ("by_rule", by_rule(MAX_RULE_GROUPS, dialect)),
                    ("time_buckets", time_buckets(&filter, 3600, 24, dialect)),
                    ("error_aggregates", error_aggregates(&filter, 20, dialect)),
                    (
                        "delete_chunk",
                        delete_chunk(&filter, Some(1_000), &["audit".to_string()], 500, dialect),
                    ),
                    (
                        "delete_all",
                        delete_all(&filter, Some(1_000), &["audit".to_string()], dialect),
                    ),
                    (
                        "count_protected_candidates",
                        count_protected_candidates(
                            &filter,
                            Some(1_000),
                            &["audit".to_string()],
                            250,
                            dialect,
                        ),
                    ),
                    (
                        "retention_delete_chunk",
                        retention_delete_chunk(&sweeps, 500, dialect),
                    ),
                    (
                        "retention_count_plan",
                        retention_count_plan(&sweeps, 250, dialect),
                    ),
                ];
                for (what, plan) in statements {
                    assert_plan_is_bindable(
                        &plan,
                        dialect,
                        &format!("{name} / {what} / {dialect:?}"),
                    );
                }
            }
        }
    }

    #[test]
    fn repeated_values_are_bound_once_per_placeholder() {
        // PostgreSQL reuses one `$n`; SQLite must supply one value per `?`, and in
        // both dialects the order has to follow the statement text — the bucket
        // width is written in the SELECT list, so it is bound first.
        let sqlite = time_buckets(&filter(), 10, 100, SqlDialect::Sqlite);
        assert_eq!(sqlite.sql.matches('?').count(), 7, "{}", sqlite.sql);
        assert_eq!(
            placeholders(&sqlite),
            vec!["10", "10", "ERROR", "WARN", "core", "100", "100"]
        );
        assert_plan_is_bindable(&sqlite, SqlDialect::Sqlite, "time_buckets/sqlite");

        let postgres = time_buckets(&filter(), 10, 100, SqlDialect::Postgres);
        assert!(
            postgres.sql.contains("SELECT (ts / $1) * $1 AS bucket"),
            "{}",
            postgres.sql
        );
        assert_eq!(postgres.params.len(), 6);
        assert_plan_is_bindable(&postgres, SqlDialect::Postgres, "time_buckets/postgres");
    }

    #[test]
    fn select_page_always_binds_limit_and_offset_unless_seeking_by_keyset() {
        let plan = select_page(
            &QueryFilter::default(),
            &Page::default(),
            SqlDialect::Postgres,
        );
        assert!(plan.sql.ends_with("LIMIT $1 OFFSET $2"), "{}", plan.sql);
        assert_eq!(plan.params, vec![Param::Int(100), Param::Int(0)]);
        let plan = select_page(
            &QueryFilter::default(),
            &Page::clamped(25, 50, false),
            SqlDialect::Sqlite,
        );
        assert!(plan.sql.ends_with("LIMIT ? OFFSET ?"), "{}", plan.sql);
        assert_eq!(plan.params, vec![Param::Int(25), Param::Int(50)]);
        // A keyset seek replaces the offset entirely.
        let keyset = select_page(
            &QueryFilter::default(),
            &Page::keyset_page(10, false, Some(Keyset::new(5, 1))),
            SqlDialect::Postgres,
        );
        assert!(!keyset.sql.contains("OFFSET"), "{}", keyset.sql);
        assert!(keyset.sql.ends_with("LIMIT $4"), "{}", keyset.sql);
        assert_plan_is_bindable(&keyset, SqlDialect::Postgres, "keyset page");
    }

    #[test]
    fn group_ties_break_on_the_first_row_not_on_the_planner() {
        for dialect in [SqlDialect::Sqlite, SqlDialect::Postgres] {
            let grouped = group_by(&QueryFilter::default(), GroupBy::Service, 10, dialect);
            assert!(
                grouped.sql.contains("ORDER BY c DESC, MIN(id) ASC LIMIT"),
                "{:?}: {}",
                dialect,
                grouped.sql
            );
            let rules = by_rule(MAX_RULE_GROUPS, dialect);
            assert!(
                rules.sql.contains("ORDER BY c DESC, MIN(id) ASC LIMIT"),
                "{:?}: {}",
                dialect,
                rules.sql
            );
        }
    }
}
