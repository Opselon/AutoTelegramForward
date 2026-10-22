//! The **real binary**, end to end: process start, handshake line, real gRPC over
//! TCP, the metrics/health listener, SIGTERM, clean exit — and then a genuine
//! process restart that proves the durable records survived.
//!
//! This is the test that catches wiring mistakes the in-process tests cannot see:
//! an empty stdout before the handshake, a missing `listening on <addr>` line, a
//! non-zero exit code after a signal, a metrics endpoint that does not answer, or a
//! record that only lived in memory.

use atf_logger::pb;
use std::io::{BufRead, BufReader};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

type Client = pb::log_control_service_client::LogControlServiceClient<tonic::transport::Channel>;

const BIN: &str = env!("CARGO_BIN_EXE_atf-logger");

struct Child {
    process: std::process::Child,
    lines: Arc<Mutex<Vec<String>>>,
    addr: String,
    metrics_addr: String,
}

impl Child {
    fn start(db_path: &str, metrics_port: u16) -> Child {
        let mut process = Command::new(BIN)
            .args([
                "--db",
                db_path,
                "--listen",
                "127.0.0.1:0",
                "--metrics-listen",
                &format!("127.0.0.1:{metrics_port}"),
                "--print-addr",
                "--no-retention",
                "--batch-delay-ms",
                "5",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("spawn atf-logger");

        let stdout = process.stdout.take().expect("stdout pipe");
        let lines = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::clone(&lines);
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                match line {
                    Ok(line) => sink.lock().unwrap().push(line),
                    Err(_) => break,
                }
            }
        });

        // The handshake must arrive quickly; a slow start means a broken binary.
        let deadline = Instant::now() + Duration::from_secs(30);
        let handshake = loop {
            if let Some(first) = lines.lock().unwrap().first().cloned() {
                break first;
            }
            if Instant::now() > deadline {
                let _ = process.kill();
                panic!("no handshake line on stdout within 30s");
            }
            std::thread::sleep(Duration::from_millis(20));
        };
        let addr = handshake
            .rsplit(' ')
            .next()
            .expect("address token")
            .to_string();
        assert!(
            handshake.starts_with("listening on "),
            "unexpected handshake line: {handshake:?}"
        );
        assert_eq!(
            lines.lock().unwrap().len(),
            1,
            "stdout must carry exactly the handshake, got {:?}",
            lines.lock().unwrap()
        );

        Child {
            process,
            lines,
            addr,
            metrics_addr: format!("127.0.0.1:{metrics_port}"),
        }
    }

    /// SIGTERM and wait, returning the exit code.
    fn terminate(&mut self) -> i32 {
        // Via the shell: `kill` is a builtin, and a minimal builder image
        // (rust:*-slim without procps) has no kill(1) on PATH -- spawning it
        // directly fails with ENOENT rather than signalling anything.
        let status = Command::new("sh")
            .arg("-c")
            .arg(format!("kill -TERM {}", self.process.id()))
            .status()
            .expect("send SIGTERM");
        assert!(status.success(), "kill -TERM failed: {status:?}");
        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            match self.process.try_wait().expect("try_wait") {
                Some(status) => {
                    return status.code().unwrap_or(-1);
                }
                None => {
                    if Instant::now() > deadline {
                        let _ = self.process.kill();
                        panic!("the binary did not exit within 30s of SIGTERM");
                    }
                    std::thread::sleep(Duration::from_millis(25));
                }
            }
        }
    }

    /// Every stdout line except the handshake: must be empty.
    fn extra_stdout(&self) -> Vec<String> {
        self.lines.lock().unwrap().iter().skip(1).cloned().collect()
    }
}

/// Pick a free port by binding and releasing it.
fn free_port() -> u16 {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind probe");
    let port = listener.local_addr().expect("addr").port();
    drop(listener);
    port
}

async fn http_get(addr: &str, path: &str) -> String {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let mut socket = tokio::net::TcpStream::connect(addr)
        .await
        .unwrap_or_else(|err| panic!("connect {addr}: {err}"));
    let request = format!("GET {path} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n\r\n");
    socket.write_all(request.as_bytes()).await.expect("write");
    let mut response = String::new();
    tokio::time::timeout(
        Duration::from_secs(10),
        socket.read_to_string(&mut response),
    )
    .await
    .expect("the metrics listener did not answer within 10s")
    .expect("read the response");
    response
}

async fn connect(addr: &str) -> Client {
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        match Client::connect(format!("http://{addr}")).await {
            Ok(client) => return client,
            Err(err) => {
                if Instant::now() > deadline {
                    panic!("could not connect to {addr}: {err}");
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        }
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
        request_id: String::new(),
        trace_id: String::new(),
        module: String::new(),
        fingerprint: String::new(),
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn binary_serves_grpc_exposes_metrics_and_restarts_durably() {
    let dir = tempfile::tempdir().expect("temp dir");
    let db_path = dir.path().join("binary.db").to_string_lossy().to_string();
    let metrics_port = free_port();

    // ---- first run: serve, accept, expose --------------------------------
    let mut child = Child::start(&db_path, metrics_port);
    let mut client = connect(&child.addr).await;

    let status = client
        .log(pb::LogRequest {
            entry: Some(entry("ERROR", "written by the real binary")),
        })
        .await
        .expect("Log RPC")
        .into_inner();
    assert!(status.success, "durable ack must be true: {status:?}");

    // A durable ack is a committed record: visible to a second connection too.
    let page = client
        .query_logs(pb::QueryLogsRequest {
            limit: 10,
            ..Default::default()
        })
        .await
        .expect("QueryLogs RPC")
        .into_inner();
    assert_eq!(page.total, 1);
    assert_eq!(page.logs[0].message, "written by the real binary");

    // ---- metrics + health ------------------------------------------------
    let metrics = http_get(&child.metrics_addr, "/metrics").await;
    assert!(metrics.starts_with("HTTP/1.1 200 OK"), "{metrics}");
    for family in [
        "atf_logger_events_received_total",
        "atf_logger_events_accepted_total",
        "atf_logger_events_persisted_total",
        "atf_logger_events_dropped_total",
        "atf_logger_info",
        "atf_logger_readiness",
        "atf_logger_liveness",
        "atf_logger_storage_up",
    ] {
        assert!(
            metrics.contains(family),
            "missing {family} in the exposition"
        );
    }
    let health = http_get(&child.metrics_addr, "/healthz").await;
    assert!(health.starts_with("HTTP/1.1 200 OK"), "{health}");
    let ready = http_get(&child.metrics_addr, "/readyz").await;
    assert!(
        ready.starts_with("HTTP/1.1 200 OK"),
        "a healthy logger must be ready: {ready}"
    );

    // ---- SIGTERM: graceful drain, clean exit -----------------------------
    let code = child.terminate();
    assert_eq!(code, 0, "a clean drain exits 0");
    assert!(
        child.extra_stdout().is_empty(),
        "stdout carried more than the handshake: {:?}",
        child.extra_stdout()
    );

    // The record is in the file, not in a process that no longer exists.
    {
        let conn = rusqlite::Connection::open(&db_path).expect("reopen the archive");
        let rows: i64 = conn
            .query_row("SELECT COUNT(*) FROM logs", [], |row| row.get(0))
            .expect("count rows");
        assert_eq!(rows, 1, "a durable ack must survive the process");
    }

    // ---- second run: the same archive, a new process ---------------------
    let mut child = Child::start(&db_path, free_port());
    let mut client = connect(&child.addr).await;
    let page = client
        .query_logs(pb::QueryLogsRequest {
            limit: 10,
            ..Default::default()
        })
        .await
        .expect("QueryLogs RPC after restart")
        .into_inner();
    assert_eq!(page.total, 1, "the record survived the restart");
    assert_eq!(page.logs[0].message, "written by the real binary");

    // And it keeps accepting new work on the old archive.
    let status = client
        .log(pb::LogRequest {
            entry: Some(entry("WARN", "after the restart")),
        })
        .await
        .expect("Log RPC after restart")
        .into_inner();
    assert!(status.success, "{status:?}");
    let page = client
        .query_logs(pb::QueryLogsRequest {
            limit: 10,
            ..Default::default()
        })
        .await
        .expect("QueryLogs RPC")
        .into_inner();
    assert_eq!(page.total, 2);

    assert_eq!(child.terminate(), 0);
}
