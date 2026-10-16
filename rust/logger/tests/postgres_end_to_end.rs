//! PostgreSQL integration tests.
//!
//! The storage layer, the query builder and the retention sweep are exercised against
//! a **real** PostgreSQL server when one is reachable. Set
//! `ATF_LOGGER_TEST_PG_DSN` (e.g. `postgres://atf:atf@127.0.0.1:55432/atf_test`) to
//! enable them; without it the test prints `SKIPPED (no ATF_LOGGER_TEST_PG_DSN)` and
//! returns, so the suite stays green on a machine with no server — an explicit skip,
//! never a silent pass.
//!
//! The unavailability tests run everywhere and pin the contract's "no silent
//! fallback" rule: a broken PostgreSQL configuration fails loudly and must never
//! leave the engine writing to SQLite instead.

use atf_logger::config::Config;
use atf_logger::metrics::Metrics;
use atf_logger::query::{GroupBy, Page, PurgeFilter, QueryFilter, RetentionSweep, StatsScope};
use atf_logger::storage::{self, Backend, BackendConfig, Record};
use std::sync::Arc;

fn dsn() -> Option<String> {
    std::env::var("ATF_LOGGER_TEST_PG_DSN")
        .ok()
        .filter(|dsn| !dsn.is_empty())
}

fn test_metrics() -> Arc<Metrics> {
    Arc::new(Metrics::new())
}

fn no_env(_: &str) -> Option<String> {
    None
}

/// Turn a DSN into the explicit backend selection the binary would use.
fn backend_config(dsn: &str) -> BackendConfig {
    let config = Config {
        db_path: dsn.to_string(),
        ..Default::default()
    };
    BackendConfig::from_config(&config, &no_env).expect("postgres selected by DSN")
}

fn record(
    ts: i64,
    level: &str,
    service: &str,
    category: &str,
    message: &str,
    corr: &str,
) -> Record {
    Record {
        id: 0,
        ts,
        level: level.to_string(),
        service: service.to_string(),
        category: category.to_string(),
        message: message.to_string(),
        detail: r#"{"source":"postgres-test"}"#.to_string(),
        rule_id: format!("rule-{service}"),
        user_id: 7,
        correlation_id: corr.to_string(),
    }
}

fn everything() -> PurgeFilter {
    PurgeFilter {
        level: String::new(),
        service: String::new(),
        category: String::new(),
        rule_id: String::new(),
        older_than_hours: 0,
        protect_categories: Vec::new(),
        now: 0,
    }
}

#[test]
fn a_broken_postgres_configuration_fails_loudly_and_never_falls_back() {
    // A closed port: the connection attempt must fail, and the failure must name
    // itself rather than opening a SQLite archive behind the operator's back.
    let backend = backend_config("postgres://atf:atf@127.0.0.1:1/atf_missing");
    assert_eq!(backend.backend, Backend::Postgres);

    let dir = tempfile::tempdir().unwrap();
    let sqlite_path = dir.path().join("must-not-be-created.db");
    let config = Config {
        db_path: sqlite_path.to_string_lossy().to_string(),
        ..Default::default()
    };

    let error = storage::open(&config, &backend, test_metrics())
        .err()
        .expect("an unreachable backend must fail startup");
    assert_eq!(error.code(), "STORAGE_UNAVAILABLE", "{error:?}");
    assert!(
        !sqlite_path.exists(),
        "a failed PostgreSQL start must not leave a SQLite archive behind"
    );
}

#[test]
fn an_unknown_backend_is_refused_not_guessed() {
    let config = Config {
        db_path: "mysql://nope/db".to_string(),
        ..Default::default()
    };
    let error =
        BackendConfig::from_config(&config, &no_env).expect_err("an unknown DSN must be refused");
    let message = error.message();
    assert!(
        message.contains("ATF_LOGGER_BACKEND") || message.contains("postgres"),
        "{error:?}"
    );
}

#[test]
fn postgres_end_to_end() {
    let Some(dsn) = dsn() else {
        eprintln!("SKIPPED (no ATF_LOGGER_TEST_PG_DSN): no PostgreSQL server reachable");
        return;
    };
    let backend = backend_config(&dsn);
    let config = Config {
        db_path: dsn,
        ..Default::default()
    };

    // ---- open + migrate + probe ------------------------------------------
    let storage = storage::open(&config, &backend, test_metrics())
        .expect("open PostgreSQL (is the server up and the schema writable?)");
    assert_eq!(storage.backend(), Backend::Postgres);
    storage.probe().expect("probe after migration");

    // Clear leftovers from an earlier run so the counts below are exact.
    storage
        .purge(&everything(), 1_000, u64::MAX)
        .expect("clear");
    assert_eq!(storage.table_rows().unwrap(), 0);

    // ---- batched insert --------------------------------------------------
    let tag = format!("pgtest-{}", std::process::id());
    let records: Vec<Record> = (0..250)
        .map(|index: i64| {
            let level = match index % 3 {
                0 => "ERROR",
                1 => "WARN",
                _ => "INFO",
            };
            let service = if index % 2 == 0 { "core" } else { "worker" };
            // Every fifth row is an audit row: the retention test below proves those
            // survive a sweep that would otherwise delete them.
            let category = if index % 5 == 0 { "AUDIT" } else { "PAYMENT" };
            record(
                1_700_000_000 + index,
                level,
                service,
                category,
                &format!("{tag} batch record {index}"),
                &format!("{tag}-{index}"),
            )
        })
        .collect();
    assert_eq!(storage.insert_batch(&records).expect("batched insert"), 250);
    assert_eq!(
        storage.table_rows().unwrap(),
        250,
        "one statement per batch commits every row"
    );

    // ---- query -----------------------------------------------------------
    let errors = QueryFilter {
        levels: vec!["ERROR".to_string()],
        ..Default::default()
    };
    let (rows, total) = storage
        .query(
            &errors,
            &Page {
                limit: 100,
                ..Default::default()
            },
        )
        .expect("query by level");
    assert_eq!(total, storage.count(&errors, None).unwrap());
    assert!(rows.len() <= 100, "the page is bounded");
    assert!(rows.iter().all(|row| row.level == "ERROR"));
    assert!(
        rows.windows(2).all(|pair| pair[0].ts >= pair[1].ts),
        "newest first by default"
    );

    let searched = QueryFilter {
        search: tag.clone(),
        ..Default::default()
    };
    assert_eq!(storage.count(&searched, None).unwrap(), 250);
    assert_eq!(
        storage.count(&searched, Some(10)).unwrap(),
        10,
        "a capped count stops at the cap instead of scanning the table"
    );

    let scoped = QueryFilter {
        user_id: 7,
        correlation_id: format!("{tag}-1"),
        ..Default::default()
    };
    let (rows, total) = storage
        .query(
            &scoped,
            &Page {
                limit: 10,
                ..Default::default()
            },
        )
        .expect("query by correlation id");
    assert_eq!(total, 1);
    assert_eq!(rows[0].correlation_id, format!("{tag}-1"));
    assert_eq!(rows[0].rule_id, "rule-worker");

    // Tenant scoping is real: another tenant sees nothing.
    let other_tenant = QueryFilter {
        user_id: 8,
        ..Default::default()
    };
    assert_eq!(storage.count(&other_tenant, None).unwrap(), 0);

    // ---- stats / aggregation ---------------------------------------------
    let whole = StatsScope {
        rule_id: String::new(),
        user_id: 0,
    };
    assert_eq!(storage.stats(&whole).unwrap().total, 250);
    let scoped_stats = storage
        .stats(&StatsScope {
            rule_id: "rule-core".to_string(),
            user_id: 0,
        })
        .unwrap();
    assert_eq!(scoped_stats.total, 125);
    let groups = storage.top_groups(&searched, GroupBy::Service, 10).unwrap();
    assert_eq!(groups.len(), 2, "two services");
    let buckets = storage.time_buckets(&searched, 60, 1_000).unwrap();
    assert!(!buckets.is_empty());
    let failures = storage.error_aggregates(&errors, 10).unwrap();
    assert!(!failures.is_empty());

    // ---- retention with a protected category -----------------------------
    let candidates = QueryFilter {
        levels: vec!["ERROR".to_string(), "WARN".to_string()],
        ..Default::default()
    };
    let expected_candidates = storage.count(&candidates, None).unwrap();
    let protected_candidates = storage
        .count(
            &QueryFilter {
                levels: vec!["ERROR".to_string(), "WARN".to_string()],
                category: "AUDIT".to_string(),
                ..Default::default()
            },
            None,
        )
        .unwrap();
    assert!(
        protected_candidates > 0,
        "the fixture must contain protected candidates"
    );
    let audit_before = storage
        .count(
            &QueryFilter {
                category: "AUDIT".to_string(),
                ..Default::default()
            },
            None,
        )
        .unwrap();

    let sweep = RetentionSweep {
        filter: candidates.clone(),
        cutoff: Some(i64::MAX),
        protected: vec!["AUDIT".to_string()],
        exclusions: Default::default(),
    };
    let report = storage
        .retention_purge(&sweep, 100, 1_000_000)
        .expect("retention sweep");
    assert_eq!(
        report.purged,
        expected_candidates - protected_candidates,
        "every purgeable candidate goes, and only those: {report:?}"
    );
    assert_eq!(
        storage
            .count(
                &QueryFilter {
                    category: "AUDIT".to_string(),
                    ..Default::default()
                },
                None,
            )
            .unwrap(),
        audit_before,
        "not one protected row was purged"
    );
    assert_eq!(
        storage.table_rows().unwrap(),
        250 - report.purged,
        "the archive only lost what the report says"
    );

    // ---- bounded work per cycle -------------------------------------------
    let remaining = QueryFilter {
        levels: vec!["INFO".to_string()],
        ..Default::default()
    };
    assert!(storage.count(&remaining, None).unwrap() > 100);
    let bounded = storage
        .retention_purge(
            &RetentionSweep {
                filter: remaining,
                cutoff: Some(i64::MAX),
                protected: Vec::new(),
                exclusions: Default::default(),
            },
            50,
            100,
        )
        .expect("bounded sweep");
    assert_eq!(bounded.purged, 100, "the cycle respects its work budget");
    let over_budget = storage
        .retention_purge(
            &RetentionSweep {
                filter: QueryFilter {
                    levels: vec!["INFO".to_string()],
                    ..Default::default()
                },
                cutoff: Some(i64::MAX),
                protected: Vec::new(),
                exclusions: Default::default(),
            },
            50,
            1,
        )
        .expect("tiny sweep");
    assert_eq!(over_budget.purged, 1, "a budget of one row deletes one row");

    // ---- cleanup ----------------------------------------------------------
    storage
        .purge(&everything(), 1_000, u64::MAX)
        .expect("cleanup");
    assert_eq!(storage.table_rows().unwrap(), 0);
    eprintln!(
        "PostgreSQL integration passed against {}",
        storage.describe()
    );
}
