//! End-to-end tests: the **real** gRPC service, driven by the **real** generated
//! client, over a **real** TCP connection, against a **real** SQLite file.
//!
//! Nothing here is mocked. The server is the same `LogControl` the binary runs,
//! started with `tonic::transport::Server::serve_with_incoming` on a loopback port;
//! the client is the generated `LogControlServiceClient`; the store is a tempfile
//! archive that outlives the server so restart/recovery can be tested.
//!
//! Covered: every supported event field, insert/query/stats/purge, migration of an
//! existing Python-created archive, concurrent readers and writers, runtime
//! settings, restart/durable recovery, redaction-before-persistence, correlation
//! propagation, and graceful stop.

use atf_logger::config::{AckMode, Config};
use atf_logger::health::HealthState;
use atf_logger::ingest::CollectPolicy;
use atf_logger::metrics::Metrics;
use atf_logger::pb;
use atf_logger::pipeline::{Pipeline, PipelineConfig};
use atf_logger::shutdown::{Shutdown, StopSignal};
use atf_logger::storage::{self, BackendConfig, Storage};
use atf_logger::transport::LogControl;
use std::sync::Arc;
use std::time::Duration;
use tokio_stream::wrappers::TcpListenerStream;

type Client = pb::log_control_service_client::LogControlServiceClient<tonic::transport::Channel>;

/// The environment these tests run with: none. Nothing about them may depend on the
/// machine's own configuration.
fn no_env(_: &str) -> Option<String> {
    None
}

/// Every RPC in this file is bounded: a hung call must fail the test, not hang the
/// suite forever.
const CALL_TIMEOUT: Duration = Duration::from_secs(20);

struct Started {
    /// Kept alive so an engine-created archive survives the server. `None` when the
    /// caller owns the archive (the restart test), which is exactly the case where
    /// the file must outlive this struct.
    _dir: Option<tempfile::TempDir>,
    db_path: String,
    client: Client,
    pipeline: Arc<Pipeline>,
    storage: Arc<dyn Storage>,
    metrics: Arc<Metrics>,
    health: Arc<HealthState>,
    stop: Option<tokio::sync::oneshot::Sender<()>>,
    /// The serving task. Its `Result` is discarded at shutdown (the test only
    /// cares that the task ends), so it is named for what it is.
    task: tokio::task::JoinHandle<Result<(), tonic::transport::Error>>,
}

impl Started {
    /// Stop the pipeline and the listener, exactly like the graceful path does.
    async fn shutdown(mut self) {
        let shutdown = Shutdown::new(
            Arc::clone(&self.storage),
            Arc::clone(&self.metrics),
            Arc::clone(&self.health),
            Duration::from_secs(10),
        );
        let outcome = shutdown.run(StopSignal::Sigterm, &self.pipeline, None);
        assert!(outcome.drained, "{outcome:?}");
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        let _ = tokio::time::timeout(Duration::from_secs(5), self.task).await;
    }
}

async fn start_with(config: Config, policy: CollectPolicy) -> Started {
    let dir = tempfile::tempdir().expect("temp archive dir");
    let db_path = dir.path().join("atf_logs.db").to_string_lossy().to_string();
    start_on_archive(config, policy, &db_path, Some(dir)).await
}

/// Start an engine on an archive the caller owns.
///
/// `dir` keeps an engine-created archive alive as long as the `Started` does, and
/// deletes it on drop. `None` means the archive outlives the server, which is the
/// only way to test a real restart: `shutdown(self)` *consumes* the `Started`, so
/// a server-owned temp archive is deleted the moment the server stops and the
/// reopen would silently create a fresh, empty file -- a durability check that
/// passes while proving nothing.
async fn start_on_archive(
    config: Config,
    policy: CollectPolicy,
    db_path: &str,
    dir: Option<tempfile::TempDir>,
) -> Started {
    let mut config = config;
    config.db_path = db_path.to_string();
    let backend = BackendConfig::from_config(&config, &no_env).expect("explicit backend config");
    let metrics = Arc::new(Metrics::new());
    let health = Arc::new(HealthState::new());
    health.mark_ready("test probe ok");
    let storage = storage::open(&config, &backend, Arc::clone(&metrics)).expect("open sqlite");
    let pipeline = Pipeline::start(
        PipelineConfig::from_config(&config),
        Arc::clone(&storage),
        Arc::clone(&metrics),
    )
    .expect("start pipeline");
    let service = LogControl::new(
        &config,
        policy,
        Arc::clone(&pipeline),
        Arc::clone(&storage),
        Arc::clone(&metrics),
        Arc::clone(&health),
    )
    .with_backend(backend.describe_safe());

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind loopback");
    let addr = listener.local_addr().expect("local addr").to_string();
    let (stop_tx, stop_rx) = tokio::sync::oneshot::channel::<()>();
    let task = tokio::spawn(
        tonic::transport::Server::builder()
            .add_service(service.into_server())
            .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async move {
                let _ = stop_rx.await;
            }),
    );
    let client = Client::connect(format!("http://{addr}"))
        .await
        .expect("connect the generated client");

    Started {
        _dir: dir,
        db_path: config.db_path.clone(),
        client,
        pipeline,
        storage,
        metrics,
        health,
        stop: Some(stop_tx),
        task,
    }
}

async fn start(config: Config) -> Started {
    start_with(config, CollectPolicy::permissive()).await
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

async fn log(client: &mut Client, entry: pb::LogEntry) -> pb::StatusResponse {
    tokio::time::timeout(
        CALL_TIMEOUT,
        client.log(pb::LogRequest { entry: Some(entry) }),
    )
    .await
    .expect("Log RPC timed out")
    .expect("Log RPC transport error")
    .into_inner()
}

async fn query(client: &mut Client, request: pb::QueryLogsRequest) -> pb::QueryLogsResponse {
    tokio::time::timeout(CALL_TIMEOUT, client.query_logs(request))
        .await
        .expect("QueryLogs RPC timed out")
        .expect("QueryLogs RPC transport error")
        .into_inner()
}

async fn stats(client: &mut Client, rule_id: &str, user_id: i64) -> pb::LogStatsResponse {
    tokio::time::timeout(
        CALL_TIMEOUT,
        client.log_stats(pb::LogStatsRequest {
            rule_id: rule_id.to_string(),
            user_id,
        }),
    )
    .await
    .expect("LogStats RPC timed out")
    .expect("LogStats RPC transport error")
    .into_inner()
}

async fn purge(client: &mut Client, request: pb::PurgeLogsRequest) -> pb::PurgeLogsResponse {
    tokio::time::timeout(CALL_TIMEOUT, client.purge_logs(request))
        .await
        .expect("PurgeLogs RPC timed out")
        .expect("PurgeLogs RPC transport error")
        .into_inner()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn insert_query_stats_purge_over_real_grpc() {
    let mut server = start(Config::default()).await;
    let client = &mut server.client;

    // ---- insert ---------------------------------------------------------
    for index in 0..30 {
        let level = match index % 3 {
            0 => "ERROR",
            1 => "WARN",
            _ => "CRITICAL",
        };
        let response = log(
            client,
            pb::LogEntry {
                ts: 1_700_000_000 + index,
                service: if index % 2 == 0 { "core" } else { "worker" }.to_string(),
                category: if index % 2 == 0 { "system" } else { "payment" }.to_string(),
                rule_id: format!("rule-{}", index % 4),
                user_id: if index % 5 == 0 { 42 } else { 0 },
                correlation_id: format!("corr-{index}"),
                ..entry(level, &format!("event {index} needle"))
            },
        )
        .await;
        assert!(response.success, "insert {index} failed: {response:?}");
        assert_eq!(response.error_code, "");
    }
    // Durable ack: everything acked is already on disk.
    assert_eq!(server.storage.table_rows().unwrap(), 30);

    // ---- query ----------------------------------------------------------
    let page = query(
        client,
        pb::QueryLogsRequest {
            limit: 10,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(page.logs.len(), 10);
    assert_eq!(page.total, 30);
    assert!(page.has_more);
    assert_eq!(page.logs[0].ts, 1_700_000_029, "newest first");

    let errors = query(
        client,
        pb::QueryLogsRequest {
            levels: vec!["ERROR".to_string(), "CRITICAL".to_string()],
            limit: 100,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(errors.total, 20);

    let searched = query(
        client,
        pb::QueryLogsRequest {
            search: "needle".to_string(),
            service: "core".to_string(),
            limit: 100,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(searched.total, 15);
    assert!(searched.logs.iter().all(|l| l.service == "core"));

    let scoped = query(
        client,
        pb::QueryLogsRequest {
            user_id: 42,
            limit: 100,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(scoped.total, 6);

    // ---- stats ----------------------------------------------------------
    let aggregate = stats(client, "", 0).await;
    assert_eq!(aggregate.total, 30);
    assert_eq!(aggregate.by_level.get("ERROR"), Some(&10));
    assert_eq!(aggregate.by_rule.get("rule-0"), Some(&8));
    assert_eq!(aggregate.oldest_ts, 1_700_000_000);
    assert_eq!(aggregate.newest_ts, 1_700_000_029);
    let rule_scoped = stats(client, "rule-1", 0).await;
    assert_eq!(rule_scoped.total, 8);
    // Still the whole-archive rule breakdown, exactly like the Python engine.
    assert_eq!(rule_scoped.by_rule.get("rule-3"), Some(&7));

    // ---- purge ----------------------------------------------------------
    let now = atf_logger::config::TsUnit::Seconds.now();
    let fresh = log(
        client,
        pb::LogEntry {
            ts: now,
            ..entry("ERROR", "fresh row")
        },
    )
    .await;
    assert!(fresh.success);
    let purged = purge(
        client,
        pb::PurgeLogsRequest {
            older_than_hours: 1,
            ..Default::default()
        },
    )
    .await;
    assert!(purged.success, "{purged:?}");
    assert_eq!(purged.purged, 30);
    assert_eq!(
        purged.remaining, 1,
        "the fresh row survives its own purge scope"
    );
    assert_eq!(server.storage.table_rows().unwrap(), 1);

    server.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn every_supported_event_field_round_trips() {
    let mut server = start(Config::default()).await;
    let sent = pb::LogEntry {
        ts: 1_700_123_456,
        // prost generates owned `String` fields, so every textual field is owned
        // here too; `detail` is a JSON blob because that is what producers send.
        level: "WARN".to_string(),
        service: "forwarder".to_string(),
        category: "delivery".to_string(),
        message: "delivery retry scheduled".to_string(),
        detail: r#"{"attempt":3,"queue":"high"}"#.to_string(),
        rule_id: "rule-77".to_string(),
        user_id: 4242,
        correlation_id: "corr-direct-1".to_string(),
    };
    let response = log(&mut server.client, sent.clone()).await;
    assert!(response.success, "{response:?}");

    let page = query(
        &mut server.client,
        pb::QueryLogsRequest {
            correlation_id: "corr-direct-1".to_string(),
            limit: 10,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(page.logs.len(), 1);
    let got = &page.logs[0];
    assert_eq!(got.ts, sent.ts);
    assert_eq!(got.level, sent.level);
    assert_eq!(got.service, sent.service);
    assert_eq!(got.category, sent.category);
    assert_eq!(got.message, sent.message);
    assert_eq!(
        got.detail, sent.detail,
        "detail is preserved verbatim when clean"
    );
    assert_eq!(got.rule_id, sent.rule_id);
    assert_eq!(got.user_id, sent.user_id);
    assert_eq!(got.correlation_id, sent.correlation_id);

    // A second record on the same correlation id: propagation is a filter, not a
    // one-shot.
    let response = log(
        &mut server.client,
        pb::LogEntry {
            message: "delivery retry succeeded".to_string(),
            ts: 1_700_123_500,
            ..sent.clone()
        },
    )
    .await;
    assert!(response.success);
    let page = query(
        &mut server.client,
        pb::QueryLogsRequest {
            correlation_id: "corr-direct-1".to_string(),
            limit: 10,
            ascending: true,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(page.logs.len(), 2);
    assert_eq!(page.logs[0].ts, 1_700_123_456);
    assert_eq!(page.logs[1].ts, 1_700_123_500);

    server.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn reads_an_existing_python_created_archive() {
    // Build the archive exactly as `logger/main.py` leaves it: the base table, the
    // three migration columns, the seven indexes — and a legacy row whose level is
    // still spelled `WARNING` the way the Python clients sent it.
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("legacy.db").to_string_lossy().to_string();
    {
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch(atf_logger::storage::sqlite::CREATE_TABLE)
            .unwrap();
        for (_, sql) in atf_logger::storage::sqlite::MIGRATION_COLUMNS {
            conn.execute_batch(sql).unwrap();
        }
        for (_, sql) in atf_logger::storage::sqlite::INDEXES {
            conn.execute_batch(sql).unwrap();
        }
        conn.execute(
            "INSERT INTO logs (ts, level, service, category, message, detail, rule_id, user_id, correlation_id)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            rusqlite::params![
                1_650_000_000i64,
                "WARNING",
                "legacy-service",
                "legacy",
                "archived by the python engine",
                r#"{"kept":true}"#,
                "legacy-rule",
                7i64,
                "legacy-corr",
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO logs (ts, level, service, category, message, detail)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![
                1_650_000_100i64,
                "ERROR",
                "legacy-service",
                "legacy",
                "old error",
                "{}"
            ],
        )
        .unwrap();
        conn.execute_batch("PRAGMA journal_mode=WAL").unwrap();
    }

    let config = Config {
        db_path: db_path.clone(),
        ..Default::default()
    };
    let backend = BackendConfig::from_config(&config, &no_env).unwrap();
    let metrics = Arc::new(Metrics::new());
    let health = Arc::new(HealthState::new());
    let storage = storage::open(&config, &backend, Arc::clone(&metrics))
        .expect("the engine must open a Python-created archive");
    let pipeline = Pipeline::start(
        PipelineConfig::from_config(&config),
        Arc::clone(&storage),
        Arc::clone(&metrics),
    )
    .unwrap();
    let service = LogControl::new(
        &config,
        CollectPolicy::permissive(),
        Arc::clone(&pipeline),
        Arc::clone(&storage),
        Arc::clone(&metrics),
        Arc::clone(&health),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    let (stop_tx, stop_rx) = tokio::sync::oneshot::channel::<()>();
    let task = tokio::spawn(
        tonic::transport::Server::builder()
            .add_service(service.into_server())
            .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async move {
                let _ = stop_rx.await;
            }),
    );
    let mut client = Client::connect(format!("http://{addr}")).await.unwrap();

    // The legacy rows are visible through the real service, unmodified.
    let page = query(
        &mut client,
        pb::QueryLogsRequest {
            service: "legacy-service".to_string(),
            limit: 100,
            ascending: true,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(page.total, 2, "both archived rows must be readable");
    let legacy = &page.logs[0];
    assert_eq!(
        legacy.level, "WARNING",
        "the archive is read, not rewritten"
    );
    assert_eq!(legacy.message, "archived by the python engine");
    assert_eq!(legacy.detail, r#"{"kept":true}"#);
    assert_eq!(legacy.rule_id, "legacy-rule");
    assert_eq!(legacy.user_id, 7);
    assert_eq!(legacy.correlation_id, "legacy-corr");

    // `LogStats` sees the archive too.
    let aggregate = stats(&mut client, "", 0).await;
    assert_eq!(aggregate.total, 2);
    assert_eq!(aggregate.by_service.get("legacy-service"), Some(&2));

    // New records land in the same table, next to the old ones.
    let response = log(
        &mut client,
        pb::LogEntry {
            ts: 1_700_000_000,
            service: "legacy-service".to_string(),
            category: "legacy".to_string(),
            ..entry("ERROR", "written after migration")
        },
    )
    .await;
    assert!(response.success, "{response:?}");
    let page = query(
        &mut client,
        pb::QueryLogsRequest {
            service: "legacy-service".to_string(),
            limit: 100,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(page.total, 3);

    let shutdown = Shutdown::new(
        Arc::clone(&storage),
        Arc::clone(&metrics),
        Arc::clone(&health),
        Duration::from_secs(10),
    );
    assert!(shutdown.run(StopSignal::Sigterm, &pipeline, None).drained);
    let _ = stop_tx.send(());
    let _ = tokio::time::timeout(Duration::from_secs(5), task).await;

    // And the Python engine would still see its own schema underneath: the
    // original table plus the migration columns.
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let columns: Vec<String> = conn
        .prepare("PRAGMA table_info(logs)")
        .unwrap()
        .query_map([], |row| row.get::<_, String>(1))
        .unwrap()
        .map(|r| r.unwrap())
        .collect();
    for expected in [
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
    ] {
        assert!(
            columns.contains(&expected.to_string()),
            "missing {expected}: {columns:?}"
        );
    }
    let indexes: Vec<String> = conn
        .prepare("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='logs'")
        .unwrap()
        .query_map([], |row| row.get::<_, String>(0))
        .unwrap()
        .map(|r| r.unwrap())
        .collect();
    for expected in [
        "idx_logs_ts",
        "idx_logs_level",
        "idx_logs_category",
        "idx_logs_service",
        "idx_logs_rule",
        "idx_logs_user",
        "idx_logs_corr",
    ] {
        assert!(
            indexes.contains(&expected.to_string()),
            "missing index {expected}: {indexes:?}"
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 6)]
async fn concurrent_readers_and_writers() {
    let mut server = start(Config::default()).await;
    let writers = 4;
    let per_writer = 25;

    let mut writer_tasks = Vec::new();
    for writer in 0..writers {
        let mut client = server.client.clone();
        writer_tasks.push(tokio::spawn(async move {
            let mut accepted = 0;
            for index in 0..per_writer {
                let response = log(
                    &mut client,
                    pb::LogEntry {
                        ts: 1_700_000_000 + (writer * per_writer + index) as i64,
                        service: format!("writer-{writer}"),
                        correlation_id: format!("w{writer}-{index}"),
                        ..entry("ERROR", &format!("concurrent {writer}/{index}"))
                    },
                )
                .await;
                assert!(response.success, "writer {writer} refused: {response:?}");
                accepted += 1;
            }
            accepted
        }));
    }

    let mut reader_tasks = Vec::new();
    for reader in 0..3 {
        let mut client = server.client.clone();
        reader_tasks.push(tokio::spawn(async move {
            for _ in 0..10 {
                let page = query(
                    &mut client,
                    pb::QueryLogsRequest {
                        limit: 100,
                        ..Default::default()
                    },
                )
                .await;
                // A read must never return something impossible: records are
                // unique within a page and the total is at least what the page
                // holds.
                let mut ids: Vec<String> = page
                    .logs
                    .iter()
                    .map(|log| log.correlation_id.clone())
                    .collect();
                ids.sort();
                assert!(
                    ids.windows(2).all(|window| window[0] != window[1]),
                    "reader {reader} saw a duplicated record"
                );
                assert!(page.total >= page.logs.len() as i32);
                tokio::time::sleep(Duration::from_millis(2)).await;
            }
        }));
    }

    let mut accepted = 0;
    for task in writer_tasks {
        accepted += task.await.expect("writer task panicked");
    }
    assert_eq!(accepted, writers * per_writer);
    for task in reader_tasks {
        task.await.expect("reader task panicked");
    }

    let page = query(
        &mut server.client,
        pb::QueryLogsRequest {
            limit: 1000,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(
        page.total,
        (writers * per_writer),
        "every acked write is readable"
    );
    assert_eq!(
        server.storage.table_rows().unwrap(),
        (writers * per_writer) as i64
    );
    assert_eq!(
        server.metrics.events_persisted.get(),
        (writers * per_writer) as u64
    );
    assert_eq!(server.metrics.events_dropped.total(), 0);

    server.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn restart_recovers_every_durable_record() {
    // Write with the default (durable) ack mode, stop, then reopen the same file with
    // a brand-new engine instance: this is what a process restart looks like.
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("restart.db").to_string_lossy().to_string();
    let config = Config {
        db_path: db_path.clone(),
        ..Default::default()
    };

    // The archive belongs to THIS test, not to the server: `shutdown(self)`
    // consumes the `Started`, and a server-owned temp archive would be deleted
    // before the reopen below -- which would then create a fresh empty file and
    // pass while proving nothing.
    let mut server =
        start_on_archive(config.clone(), CollectPolicy::permissive(), &db_path, None).await;
    for index in 0..50 {
        let response = log(
            &mut server.client,
            pb::LogEntry {
                ts: 1_700_000_000 + index,
                correlation_id: format!("durable-{index}"),
                ..entry("ERROR", &format!("durable {index}"))
            },
        )
        .await;
        assert!(
            response.success,
            "durable write {index} refused: {response:?}"
        );
    }
    assert_eq!(
        server.storage.table_rows().unwrap(),
        50,
        "all 50 committed before the stop"
    );
    server.shutdown().await;

    // Reopen the archive the first server left behind (same config, same path).

    let backend = BackendConfig::from_config(&config, &no_env).unwrap();
    let metrics = Arc::new(Metrics::new());
    let storage = storage::open(&config, &backend, Arc::clone(&metrics)).expect("reopen archive");
    assert_eq!(
        storage.table_rows().unwrap(),
        50,
        "durable records survive a restart"
    );
    let (rows, total) = storage
        .query(
            &atf_logger::query::QueryFilter::default(),
            &atf_logger::query::Page {
                limit: 100,
                ..Default::default()
            },
        )
        .unwrap();
    assert_eq!(total, 50);
    assert_eq!(rows.len(), 50);
    assert_eq!(rows[0].ts, 1_700_000_049, "newest first after a restart");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn redaction_happens_before_persistence() {
    let mut server = start(Config::default()).await;
    let secret_message = "token=SECRET-VALUE-1234567 login failed";
    let secret_detail = r#"{"password":"hunter2","session":"1BVtsOKlukZZZ1234567890","nested":{"api_hash":"deadbeefdeadbeef"}}"#;
    let response = log(
        &mut server.client,
        pb::LogEntry {
            message: secret_message.to_string(),
            detail: secret_detail.to_string(),
            correlation_id: "corr-redact".to_string(),
            ..entry("ERROR", "unused")
        },
    )
    .await;
    assert!(response.success, "{response:?}");

    // Nothing that could reconstruct a credential is readable — through the wire and
    // in the raw file the engine actually wrote.
    let page = query(
        &mut server.client,
        pb::QueryLogsRequest {
            correlation_id: "corr-redact".to_string(),
            limit: 10,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(page.logs.len(), 1);
    let row = &page.logs[0];
    for forbidden in [
        "SECRET-VALUE-1234567",
        "hunter2",
        "1BVtsOKlukZZZ1234567890",
        "deadbeefdeadbeef",
    ] {
        assert!(
            !row.message.contains(forbidden) && !row.detail.contains(forbidden),
            "{forbidden} survived redaction: {row:?}"
        );
    }
    assert!(row.message.contains("[REDACTED]"), "{}", row.message);

    let raw = std::fs::read_to_string(&server.db_path).unwrap_or_default();
    let raw_bytes = std::fs::read(&server.db_path).expect("the archive must exist to be searched");
    assert!(
        !raw_bytes.is_empty(),
        "the archive is empty; a missing file would make the checks below vacuous"
    );
    let raw_text = format!("{raw}{}", String::from_utf8_lossy(&raw_bytes));
    assert!(
        !raw_text.contains("SECRET-VALUE-1234567"),
        "the secret is in the archive file"
    );
    assert!(
        !raw_text.contains("hunter2"),
        "the password is in the archive file"
    );

    server.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn graceful_stop_refuses_new_work_and_drains_the_queue() {
    // Queued acks so the 20 records really are in the lanes when the drain starts.
    // Under the default durable mode every ack returns only once its batch has
    // committed, so the queues are necessarily empty and `drained_records` can
    // only ever be 0 -- the property this test exists to check (records in flight
    // are committed rather than lost) would be untestable.
    let config = Config {
        ack_mode: AckMode::Queued,
        batch_delay_ms: 5_000,
        ..Default::default()
    };
    let server = start(config).await;
    let mut client = server.client.clone();
    // Fill the pipeline, then drain it.
    for index in 0..20 {
        let response = log(
            &mut client,
            pb::LogEntry {
                ts: 1_700_000_000 + index,
                ..entry("ERROR", &format!("pre-drain {index}"))
            },
        )
        .await;
        assert!(response.success);
    }
    let shutdown = Shutdown::new(
        Arc::clone(&server.storage),
        Arc::clone(&server.metrics),
        Arc::clone(&server.health),
        Duration::from_secs(10),
    );
    let outcome = shutdown.run(StopSignal::Sigint, &server.pipeline, None);
    assert!(outcome.clean(), "{outcome:?}");
    assert_eq!(outcome.drained_records, 20);
    assert_eq!(server.storage.table_rows().unwrap(), 20);

    // New work is refused with a code the producer can act on.
    let refused = log(
        &mut client,
        pb::LogEntry {
            ..entry("ERROR", "after the drain")
        },
    )
    .await;
    assert!(!refused.success);
    assert_eq!(refused.error_code, "SHUTTING_DOWN");
    assert!(refused.message.contains("shutting down"), "{refused:?}");
    // Readiness is down, so a durable producer's load balancer can route away.
    assert!(!server.health.is_ready());
    assert!(!server.health.is_alive());

    // Reads keep working after admission closes: querying an archive is not ingestion.
    let page = query(
        &mut client,
        pb::QueryLogsRequest {
            limit: 100,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(page.total, 20);

    let mut server = server;
    if let Some(stop) = server.stop.take() {
        let _ = stop.send(());
    }
    let _ = tokio::time::timeout(Duration::from_secs(5), server.task).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn queued_ack_mode_answers_immediately_and_is_marked_degraded() {
    let config = Config {
        ack_mode: AckMode::Queued,
        ..Default::default()
    };
    let mut server = start(config).await;
    let response = log(
        &mut server.client,
        pb::LogEntry {
            ..entry("ERROR", "queued ack")
        },
    )
    .await;
    assert!(response.success);
    assert!(
        response.message.contains("queued"),
        "the ack must not claim durability in queued mode: {response:?}"
    );
    // The drain is what makes it crash-safe, not the ack.
    let shutdown = Shutdown::new(
        Arc::clone(&server.storage),
        Arc::clone(&server.metrics),
        Arc::clone(&server.health),
        Duration::from_secs(10),
    );
    assert!(shutdown
        .run(StopSignal::Sigterm, &server.pipeline, None)
        .clean());
    assert_eq!(server.storage.table_rows().unwrap(), 1);
    let mut server = server;
    if let Some(stop) = server.stop.take() {
        let _ = stop.send(());
    }
    let _ = tokio::time::timeout(Duration::from_secs(5), server.task).await;
}
