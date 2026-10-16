//! `atf-logger` — the process.
//!
//! Order of operations (each step fails loudly, none of them guesses):
//!
//! 1. parse + validate configuration (`--help`/`--version` are handled here),
//! 2. install stderr diagnostics,
//! 3. decide the storage backend from explicit configuration and **open it** —
//!    there is no fallback to a different backend, ever (docs/logging-contract.md
//!    §4: a Logger quietly writing to the wrong database is worse than one that
//!    refuses to start),
//! 4. print the one and only stdout line, `listening on <addr>`, as soon as the gRPC
//!    listener is bound (the benchmark harness parses the last token of the first
//!    stdout line, so nothing else may precede it and nothing else may follow it),
//! 5. serve gRPC + the metrics/health listener,
//! 6. on SIGINT/SIGTERM: stop accepting, drain, checkpoint, exit.
//!
//! Everything this binary says, except that single line, goes to stderr.

use atf_logger::config::{Config, HELP};
use atf_logger::health::{self, HealthState, REASON_SCHEMA_UNUSABLE, REASON_STORAGE_UNAVAILABLE};
use atf_logger::ingest::CollectPolicy;
use atf_logger::metrics::Metrics;
use atf_logger::pipeline::{Pipeline, PipelineConfig};
use atf_logger::retention::{Retention, RetentionPolicy};
use atf_logger::shutdown::{self, Shutdown, StopSignal};
use atf_logger::storage::{self, Backend, BackendConfig};
use atf_logger::transport::LogControl;
use atf_logger::VERSION;
use std::io::Write;
use std::sync::Arc;
use std::time::Duration;
use tokio_stream::wrappers::TcpListenerStream;

/// Configuration or CLI misuse.
const EXIT_CONFIG: i32 = 2;
/// The selected storage backend could not be opened. Never followed by a fallback.
const EXIT_STORAGE: i32 = 3;
/// A listener could not be bound.
const EXIT_BIND: i32 = 4;
/// Drained, but not cleanly (records lost, or a step failed).
const EXIT_DIRTY_SHUTDOWN: i32 = 1;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let config = match Config::from_parts(&args, &|key| std::env::var(key).ok()) {
        Ok(config) => config,
        Err(err) if err.key == "--help" => {
            println!("{HELP}");
            return;
        }
        Err(err) if err.key == "--version" => {
            println!("atf-logger {VERSION}");
            return;
        }
        Err(err) => {
            health::emergency_fields(
                "config",
                &[("key", err.key.as_str()), ("message", err.message.as_str())],
            );
            std::process::exit(EXIT_CONFIG);
        }
    };

    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .thread_name("atf-logger-rt")
        .build()
    {
        Ok(runtime) => runtime,
        Err(err) => {
            health::emergency("runtime", &format!("cannot start the async runtime: {err}"));
            std::process::exit(EXIT_CONFIG);
        }
    };

    let code = runtime.block_on(run(config));
    // Drop the runtime before exiting so no worker thread outlives the drains.
    runtime.shutdown_timeout(Duration::from_secs(1));
    std::process::exit(code);
}

fn init_diagnostics() {
    // Stderr only. stdout belongs to the handshake line.
    use tracing_subscriber::EnvFilter;
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .with_target(false)
        .try_init();
}

async fn run(config: Config) -> i32 {
    init_diagnostics();
    let metrics = Arc::new(Metrics::new());
    let health = Arc::new(HealthState::new());
    health.mark_starting("configuration validated");
    metrics.liveness.set(1);
    metrics.startup_ok.set(0);

    // ---- storage ---------------------------------------------------------
    let backend_config = match BackendConfig::from_config(&config, &|key| std::env::var(key).ok()) {
        Ok(backend) => backend,
        Err(err) => {
            health.mark_not_ready(REASON_STORAGE_UNAVAILABLE, err.to_string());
            health::emergency_fields(
                "storage_config_invalid",
                &[("code", err.code()), ("detail", err.message().as_str())],
            );
            return EXIT_CONFIG;
        }
    };
    let storage = match storage::open(&config, &backend_config, Arc::clone(&metrics)) {
        Ok(storage) => storage,
        Err(err) => {
            // No fallback branch on purpose. The process refuses to start rather
            // than write somewhere its operator did not configure.
            health.mark_not_ready(REASON_STORAGE_UNAVAILABLE, err.to_string());
            metrics.storage_up.set(0);
            health::emergency_fields(
                "storage_open_failed",
                &[
                    ("backend", backend_config.backend.as_str()),
                    ("code", err.code()),
                    ("detail", err.message().as_str()),
                    ("config", backend_config.describe_safe().as_str()),
                ],
            );
            return EXIT_STORAGE;
        }
    };
    metrics.storage_up.set(1);
    if let Err(err) = storage.probe() {
        health.mark_not_ready(REASON_SCHEMA_UNUSABLE, err.to_string());
        metrics.storage_up.set(0);
        metrics.storage_probe_failures.inc();
        health::emergency_fields(
            "storage_probe_failed",
            &[("code", err.code()), ("detail", err.message().as_str())],
        );
        return EXIT_STORAGE;
    }

    // ---- pipeline --------------------------------------------------------
    // SQLite gets exactly one writer: batching, not a second writer, is what raises
    // its throughput. PostgreSQL tolerates bounded ingestion parallelism.
    let writers = match backend_config.backend {
        Backend::Sqlite => 1,
        Backend::Postgres => backend_config.read_pool_size.clamp(1, 4),
    };
    let pipeline_config = PipelineConfig::from_config(&config).with_writers(writers);
    let pipeline = match Pipeline::start(
        pipeline_config.clone(),
        Arc::clone(&storage),
        Arc::clone(&metrics),
    ) {
        Ok(pipeline) => pipeline,
        Err(err) => {
            health.mark_not_ready(REASON_STORAGE_UNAVAILABLE, err.to_string());
            health::emergency("pipeline", &format!("cannot start the writers: {err}"));
            return EXIT_STORAGE;
        }
    };

    // ---- retention -------------------------------------------------------
    let policy =
        CollectPolicy::from_env(&config.protected_categories, &|key| std::env::var(key).ok());
    let mut retention = Retention::start(
        RetentionPolicy::from_config(&config),
        Arc::clone(&storage),
        Arc::clone(&metrics),
    );

    // ---- service ---------------------------------------------------------
    let backend_label = backend_config.describe_safe();
    let service = LogControl::new(
        &config,
        policy.clone(),
        Arc::clone(&pipeline),
        Arc::clone(&storage),
        Arc::clone(&metrics),
        Arc::clone(&health),
    )
    .with_backend(backend_label.clone());
    service.mark_ack_mode(&health);

    // ---- listeners -------------------------------------------------------
    let grpc_listener = match tokio::net::TcpListener::bind(&config.listen).await {
        Ok(listener) => listener,
        Err(err) => {
            health::emergency("bind", &format!("cannot bind {}: {err}", config.listen));
            return EXIT_BIND;
        }
    };
    let grpc_addr = match grpc_listener.local_addr() {
        Ok(addr) => addr.to_string(),
        Err(err) => {
            health::emergency("bind", &format!("cannot read the gRPC address: {err}"));
            return EXIT_BIND;
        }
    };
    let metrics_listener = match tokio::net::TcpListener::bind(&config.metrics_listen).await {
        Ok(listener) => listener,
        Err(err) => {
            health::emergency(
                "bind",
                &format!("cannot bind {}: {err}", config.metrics_listen),
            );
            return EXIT_BIND;
        }
    };
    let metrics_addr = metrics_listener
        .local_addr()
        .map(|addr| addr.to_string())
        .unwrap_or_else(|_| config.metrics_listen.clone());

    // The handshake. First line, exactly one line, flushed immediately: the
    // benchmark harness reads it and takes the last token as the address.
    println!("listening on {grpc_addr}");
    let _ = std::io::stdout().flush();

    // ---- serve -----------------------------------------------------------
    health.mark_ready(format!("storage={backend_label}"));
    metrics.readiness.set(1);
    metrics.ready.set(1);
    metrics.startup_ok.set(1);
    health::note(
        "startup",
        &format!(
            "version={VERSION} config_version={} grpc={grpc_addr} metrics={metrics_addr} \
             storage=[{}] pipeline=[{}] collection=[{}] ack_mode={} retention={}",
            config.version_hash(),
            backend_label,
            pipeline_config.describe(),
            policy.describe(),
            config.ack_mode.as_str(),
            if config.retention_enabled {
                format!(
                    "every {}s, default {}h",
                    config.retention_interval_secs, config.retention_default_max_age_hours
                )
            } else {
                "disabled".to_string()
            }
        ),
    );

    let (stop_tx, stop_rx) = tokio::sync::oneshot::channel::<()>();
    let grpc_server = tonic::transport::Server::builder()
        .add_service(service.into_server())
        .serve_with_incoming_shutdown(TcpListenerStream::new(grpc_listener), async move {
            let _ = stop_rx.await;
        });
    // Kept alive for the process lifetime; the runtime shuts it down on exit.
    let _metrics_task = tokio::spawn(atf_logger::metrics::serve(
        metrics_listener,
        Arc::clone(&metrics),
        Arc::clone(&health),
        config.version_hash(),
        backend_label.clone(),
        config.ack_mode.as_str().to_string(),
    ));
    let mut server_task = tokio::spawn(async move {
        if let Err(err) = grpc_server.await {
            health::emergency("grpc", &format!("server stopped with an error: {err}"));
            return false;
        }
        true
    });

    // ---- wait for a stop -------------------------------------------------
    let signal = tokio::select! {
        signal = shutdown::wait_for_signal() => signal,
        result = &mut server_task => {
            match result {
                Ok(true) => health::emergency("grpc", "server stopped unexpectedly; draining"),
                _ => health::emergency("grpc", "server task failed; draining"),
            }
            StopSignal::Sigterm
        }
    };
    // Stop accepting immediately: the listener is already bound, so refusing new
    // work is what keeps the drain bounded.
    let _ = stop_tx.send(());
    // A second signal must not wait for the drain.
    let _escape = shutdown::force_exit_on_second_signal();

    let shutdown = Shutdown::new(
        Arc::clone(&storage),
        Arc::clone(&metrics),
        Arc::clone(&health),
        Duration::from_millis(config.drain_timeout_ms.max(1)),
    );
    let pipeline_for_drain = Arc::clone(&pipeline);
    // The drain is blocking (thread joins, WAL checkpoint): keep it off the async
    // worker threads.
    let outcome = tokio::task::spawn_blocking(move || {
        shutdown.run(signal, &pipeline_for_drain, Some(&mut retention))
    })
    .await;

    // Let in-flight RPCs finish and the metrics listener drain.
    let _ = tokio::time::timeout(Duration::from_secs(1), server_task).await;
    // The metrics listener is left running until the runtime shuts down, so a final
    // scrape during the drain still answers (readiness is already 0 by then).

    match outcome {
        Ok(outcome) => {
            health::note("shutdown", &outcome.describe());
            if outcome.clean() {
                0
            } else {
                EXIT_DIRTY_SHUTDOWN
            }
        }
        Err(join_error) => {
            health::emergency("shutdown", &format!("drain task failed: {join_error}"));
            EXIT_DIRTY_SHUTDOWN
        }
    }
}

#[cfg(test)]
mod tests {
    // The binary's own logic is exercised end-to-end by `tests/grpc_end_to_end.rs`
    // (which spawns this binary and speaks real gRPC to it). What is worth pinning
    // here is the exit-code contract and the pass-through of `--help`/`--version`,
    // because a wrapper script or a supervisor depends on both.
    use super::*;

    #[test]
    fn help_and_version_are_config_errors_with_their_own_keys() {
        let no_env = |_: &str| -> Option<String> { None };
        let help = Config::from_parts(&["--help".to_string()], &no_env).unwrap_err();
        assert_eq!(help.key, "--help");
        assert!(help.message.contains("USAGE: atf-logger"));
        let version = Config::from_parts(&["--version".to_string()], &no_env).unwrap_err();
        assert_eq!(version.key, "--version");
        assert!(version.message.contains(VERSION));
    }

    #[test]
    fn exit_codes_are_distinct_and_documented() {
        assert_ne!(EXIT_CONFIG, EXIT_STORAGE);
        assert_ne!(EXIT_STORAGE, EXIT_BIND);
        assert_ne!(EXIT_BIND, EXIT_DIRTY_SHUTDOWN);
        assert_eq!(EXIT_CONFIG, 2);
    }

    #[test]
    fn an_unknown_flag_is_refused() {
        let no_env = |_: &str| -> Option<String> { None };
        let err = Config::from_parts(&["--nope".to_string()], &no_env).unwrap_err();
        assert_eq!(err.key, "--nope");
        assert!(err.message.contains("unknown flag"));
    }
}
