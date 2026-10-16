# `atf-logger` — the Rust logging engine

The internal logging gRPC service of ATF (`LogControlService`), with its own storage,
query, retention, metrics and health. It replaces `logger/main.py` while keeping the
wire contract the Python and Go clients already speak, and keeping the on-disk SQLite
archive readable by the old engine.

**Boundaries (non-negotiable).** Rust implements *only* this internal service. There
is no public REST API here, no business logic, and no ATF domain behaviour: the only
HTTP listener is the metrics/health endpoint of the logger itself. Go remains the
exclusive owner of the public REST API and authorization; Python Core keeps
forwarding/domain behaviour.

## Build and test

There is no Rust toolchain on the host. Everything runs in Docker:

```bash
docker volume create atf-cargo-reg atf-cargo-git atf-cargo-target   # once

docker run --rm \
  -v /root/AutoTelegramForward/rust:/w \
  -v atf-cargo-reg:/usr/local/cargo/registry \
  -v atf-cargo-git:/usr/local/cargo/git \
  -v atf-cargo-target:/target -e CARGO_TARGET_DIR=/target \
  -w /w/logger rust:1.90-slim-bookworm bash -lc '
    export PATH=/usr/local/cargo/bin:$PATH
    apt-get update -qq && apt-get install -y -qq protobuf-compiler pkg-config libssl-dev cmake >/dev/null
    cargo fmt --check
    cargo clippy --all-targets --all-features -- -D warnings
    cargo test --all-targets'
```

The first build compiles the whole dependency tree and can take 15–25 minutes; the
named volumes make every later run incremental. `protobuf-compiler` is required by
`build.rs` (tonic-build over `proto/logger.proto`).

Integration tests that need a server are explicit about it:

```bash
# PostgreSQL (optional). Starts a throwaway server, then runs the same suite with the
# live integration test enabled.
docker network create atf-test-net 2>/dev/null || true
docker run -d --rm --name atf-pg-test --network atf-test-net \
  -e POSTGRES_PASSWORD=atf -e POSTGRES_USER=atf -e POSTGRES_DB=atf_test postgres:16-alpine
# ...then add to the cargo test invocation:
#   -e ATF_LOGGER_TEST_PG_DSN=postgres://atf:atf@atf-pg-test:5432/atf_test
#   --network atf-test-net
# Without ATF_LOGGER_TEST_PG_DSN the PostgreSQL test prints
# "SKIPPED (no ATF_LOGGER_TEST_PG_DSN)" and returns.
```

## Configuration

Every flag has an `ATF_LOGGER_*` environment equivalent; flags win. `--help` prints
the authoritative list.

| Flag | Env | Default | Meaning |
| --- | --- | --- | --- |
| `--db <PATH>` | `ATF_LOGGER_DB_PATH` | `data/atf_logs.db` | SQLite archive, or a `postgres://` DSN |
| `--listen <ADDR>` | `ATF_LOGGER_LISTEN` | `0.0.0.0:50052` | gRPC bind address |
| `--metrics-listen <ADDR>` | `ATF_LOGGER_METRICS_LISTEN` | `127.0.0.1:9109` | metrics/health bind address |
| `--print-addr` | — | off | accepted for compatibility; the handshake line is always printed |
| `--queue-capacity <N>` | `ATF_LOGGER_QUEUE_CAPACITY` | 8192 | total bounded queue slots |
| `--priority-capacity <N>` | `ATF_LOGGER_PRIORITY_CAPACITY` | 2048 | slots reserved for WARN/ERROR/CRITICAL |
| `--batch-size <N>` | `ATF_LOGGER_BATCH_SIZE` | 512 | records per storage transaction |
| `--batch-delay-ms <N>` | `ATF_LOGGER_BATCH_DELAY_MS` | 2 | longest a batch waits before flush |
| `--poll-interval-ms <N>` | `ATF_LOGGER_POLL_INTERVAL_MS` | 25 | idle writer poll |
| `--ack-mode <durable\|queued>` | `ATF_LOGGER_ACK_MODE` | `durable` | see below |
| `--ack-timeout-ms <N>` | `ATF_LOGGER_ACK_TIMEOUT_MS` | 10000 | deadline for a durable ack |
| `--busy-timeout-ms <N>` | `ATF_LOGGER_BUSY_TIMEOUT_MS` | 5000 | SQLite `busy_timeout` |
| `--read-pool-size <N>` | `ATF_LOGGER_READ_POOL_SIZE` | 2 | read connections (and PostgreSQL pool size) |
| `--max-retries <N>` | `ATF_LOGGER_MAX_RETRIES` | 5 | bounded write retries before a batch fails |
| `--retry-base-ms <N>` | `ATF_LOGGER_RETRY_BASE_MS` | 20 | exponential backoff base |
| `--drain-timeout-ms <N>` | `ATF_LOGGER_DRAIN_TIMEOUT_MS` | 15000 | graceful shutdown budget |
| `--purge-timeout-ms <N>` | `ATF_LOGGER_PURGE_TIMEOUT_MS` | 120000 | deadline for `PurgeLogs` |
| `--max-message-bytes/-detail/-tag` | `ATF_LOGGER_MAX_*` | 1 MiB / 1 MiB / 4 KiB | truncation limits (counted, never silent) |
| `--redact-identifiers` / `--no-redact-identifiers` | `ATF_LOGGER_REDACT_IDENTIFIERS` | on | also redact *credential-shaped* `rule_id`/`correlation_id` values (bearer headers, bot tokens, long hex, session blobs). Ordinary ids — UUIDs, prefixed ids, anything with a digit run — are stored verbatim, because these columns are lookup keys and the Python engine stores them verbatim: masking a digit run once made every `QueryLogs` by correlation id return nothing |
| `--dedupe-window-secs <N>` | `ATF_LOGGER_DEDUPE_WINDOW_SECS` | 0 (off) | suppress duplicate records; duplicates are counted, not silent |
| `--retention` / `--no-retention` | `ATF_LOGGER_RETENTION` | on | retention worker |
| `--retention-interval-secs <N>` | `ATF_LOGGER_RETENTION_INTERVAL_SECS` | 300 | cycle period |
| `--retention-max-age-hours <N>` | `ATF_LOGGER_RETENTION_MAX_AGE_HOURS` | 168 | default age |
| `--retention-level NAME=HOURS` | — | — | per-severity override (repeatable) |
| `--retention-category NAME=HOURS` | — | — | per-category override (repeatable, `service:NAME=HOURS` allowed) |
| `--protect-category NAME` | — | `audit`, `security` | never age-purged (repeatable) |
| `--retention-ts-unit <s\|ms>` | — | `s` | unit of the stored `ts` column |

`ATF_LOGGER_BACKEND=sqlite|postgres`, `ATF_LOGGER_PG_DSN` (or `ATF_LOGGER_PG_URL`)
select the backend when the archive path is not itself a DSN.

Exit codes: `0` clean stop, `1` drained with loss or a failed step, `2` bad
configuration, `3` the selected storage backend could not be opened, `4` a listener
could not be bound.

## Storage backends

Both are first-class; **the backend is chosen by explicit configuration only**. If the
selected backend cannot be opened or migrated, the process exits non-zero naming the
failure — it never falls back to another backend. A logger quietly writing somewhere
its operator did not configure is worse than one that refuses to start.

* **SQLite** (`--db /path/atf_logs.db`) — the same `logs` table, the same seven
  indexes with the same names, and `ALTER TABLE ... ADD COLUMN ... NOT NULL DEFAULT`
  migrations, so the archives the Python engine created keep working in place.
  `journal_mode=WAL`, `synchronous=FULL` (a durable ack must survive a power loss, not
  just a process exit), `busy_timeout` from configuration. One writer connection
  because batching, not parallelism, is what raises SQLite throughput; a pool of read
  connections for queries.
* **PostgreSQL** (`postgres://user:pass@host:5432/dbname`, or
  `ATF_LOGGER_BACKEND=postgres` + `ATF_LOGGER_PG_DSN`) — a bounded client pool,
  statement/idle-in-transaction timeouts, multi-row batched inserts (chunked inside one
  transaction), and retries restricted to SQLSTATEs where **the transaction did not
  commit** (`40001`, `40P01`, `55P03`, `53300`, `57P0x`, `0800x`), so a retry cannot
  duplicate a record.

## Acknowledgement semantics

`LogControlService.Log` answers `StatusResponse`. `success == true` means exactly what
`--ack-mode` says, and nothing more:

* **`durable` (default)** — the record is committed to storage. The response is only
  sent after the batch containing it returned from the storage layer. A storage
  failure answers `success=false` with a `STORAGE_*` code.
* **`queued`** — the record is in the bounded queue, not on disk. Readiness is reported
  degraded with reason `ack_mode_queued` and the response says so, so a producer that
  needs durability can tell the difference.

Error codes on a refused record: `INVALID`, `FILTERED` (collection policy),
`OVERLOAD` (queue full after a bounded wait), `SHUTTING_DOWN`, `DUPLICATE`,
`TIMEOUT`, `STORAGE_*`. Nothing is refused silently, and nothing is dropped silently —
every refusal and every loss has a counter.

## Collection policy

Severity defaults follow `docs/logging-contract.md`: `WARN`, `ERROR` and `CRITICAL`
are collected; `INFO` and `SUCCESS` are not; `DEBUG` and `TRACE` need explicit
enablement via `ATF_LOGGER_COLLECT_LEVELS`. Protected/audit categories are exempt from
threshold filtering, and security-relevant events are never dropped merely because of
a severity threshold. The legacy alias `WARNING` is canonicalised to `WARN`.

## Redaction

Redaction happens **before persistence**, on the ingest path, for every field the
producer can fill — a producer bug must not be able to store a bot token, API hash,
auth code, session string, password, phone code or `Authorization`/cookie header.
`src/redaction.rs` is a field-aware port of `logger/redaction.py`; byte-for-byte parity
of the text pass is pinned by `tests/fixtures/redaction_parity.json`, generated from
the Python implementation. `redact_strong` is used on the ingest path: a strict
superset that also closes the Python regex's scheme hole (e.g. `Authorization: basic
<base64>`).

## Health, readiness and metrics

`--metrics-listen` (loopback by default) exposes:

* `GET /metrics` — Prometheus text format: ingestion
  (`events_received/accepted/rejected/filtered/persisted/dropped/retried/duplicate`),
  queue and workers (depth, capacity, utilization, wait, batch size/flush duration,
  pending writes, batches in flight, drain state), storage (write/read latency,
  transaction failures, pool utilization/acquisition wait, busy errors, probe
  failures, rows, retention progress/failures) and service (RPC by method and bounded
  status, latency histograms, in-flight, timeouts/cancellations, redaction time,
  config version, startup status). Label cardinality is bounded: user ids, message
  ids, rule ids and correlation ids never appear in a label.
* `GET /healthz` — **liveness**: the process is alive.
* `GET /readyz` — **readiness**: the service can actually fulfil its advertised role.
  A running process with a broken database is `not ready` with a bounded reason code
  (`storage_unavailable`, `schema_unusable`, `pipeline_stopped`, `ack_mode_queued`,
  `draining`, …), because a load balancer that keeps sending durable traffic to a
  logger that cannot commit anything is a data-loss machine. `503` when not ready.

Startup, fatal and health diagnostics go to **stderr** through a minimal,
non-recursive path that works when storage is broken. **stdout carries exactly one
line** — `listening on <addr>` — printed once the gRPC listener is bound, with the
real port when the configured port is 0. The benchmark harness parses that line.

## Retention

Age-based, per level/category/service, first match wins:
`--retention-level` → `--retention-category` (including `service:NAME=HOURS`) →
`--retention-max-age-hours`. Each sweep excludes what an earlier selector already owns
and the union of sweeps covers the table, so one row is judged by exactly one rule.
Work per cycle is bounded by a row budget; a cycle that stops early says so
(`retention_budget_exhausted_total`). Protected categories are excluded **inside the
deleting statement**, so an audit row cannot be purged by any sweep — that is a hard
rule, not a check-then-delete window.

## Tests

* Unit tests per module (`cargo test --lib`): redaction parity, ingest/normalisation,
  SQL construction for both dialects, backend selection (no fallback), SQLite schema
  and migrations, PostgreSQL statement/retry rules, metrics exposition and label
  cardinality, health states, retention ordering, pipeline admission/batching/acks,
  shutdown ordering.
* `tests/redaction_fixture.rs` — byte-for-byte parity with `logger/redaction.py` over a
  generated fixture.
* `tests/grpc_end_to_end.rs` — the real service, the real generated client, a real
  TCP connection, a real SQLite file: every supported event field, insert/query/stats/
  purge, reading an archive created with the Python schema, concurrent readers and
  writers, restart/durable recovery, runtime settings, redaction before persistence,
  correlation propagation, graceful stop.
* `tests/binary_end_to_end.rs` — spawns the compiled binary, reads the handshake line,
  speaks gRPC to it, scrapes `/metrics`/`/healthz`/`/readyz`, sends `SIGTERM`, checks a
  clean exit with no extra stdout, and restarts it on the same archive.
* `tests/postgres_end_to_end.rs` — live PostgreSQL when `ATF_LOGGER_TEST_PG_DSN` is
  set; the "no silent fallback" tests always run.

## Known limitations

* **No TLS for PostgreSQL.** The client is built with `default-features = false`
  (no OpenSSL dependency). Use a local socket, a private network, or a TLS terminator.
* **No authentication on the internal gRPC service or the metrics listener**, matching
  the Python engine it replaces. Bind both to a trusted network/loopback; the metrics
  listener is deliberately reachable even when storage is broken.
* **Search is substring matching** over `message` and `detail` (`LIKE`/`ILIKE`) with
  ordinary B-tree indexes. There is no FTS5/`pg_trgm` index, so a very selective
  substring search on a huge archive may scan. Bounded queries (capped `count`,
  `limit`) are used on hot paths so this cannot become an unbounded table scan; a
  trigram or FTS index is the next step if search volume grows.
* **Keyset pagination exists in the storage layer but the gRPC contract still exposes
  `offset`**, because the existing Python and Go clients depend on it. The engine keeps
  that contract; `export_chunk` uses keyset paging internally for bounded-memory
  exports.
* **The metrics listener is hand-rolled HTTP/1.1** (`GET /metrics`, `/healthz`,
  `/readyz`, one request per connection, no keep-alive, no gzip). It has no
  dependencies beyond `tokio`, on purpose: it must answer when nothing else works.
* **`dedupe_window_secs` is off by default**, and the window is in-memory: it survives
  neither a restart nor a second process. Enabling it makes the logger suppress a
  duplicate a producer sent twice; it is a guard against producer retries, not a
  distributed exactly-once guarantee.
* **Retention granularity** is the configured cycle interval, and cycles run in one
  process with a bounded row budget. A very large archive is cleaned over several
  cycles rather than in one long transaction (which would block writes).
* **Not measured here:** sustained million-event-per-second ingest on production
  hardware. The engine is bounded by construction and batched, but throughput figures
  from the benchmark harness must be produced by that harness on the target host.
