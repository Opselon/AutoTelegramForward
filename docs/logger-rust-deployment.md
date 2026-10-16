# Rust Logger engine — deployment runbook

## Status: not the default

The Rust engine (`rust/logger`) is an **opt-in** service. The active logging
service remains the Python logger (`atf-logger`, `Dockerfile.logger`, archive
`/app/data/logger.db`). Nothing in the default `docker compose up -d` path
starts the Rust engine, and the two services never share an archive volume.

`docker compose config` (no profile) must not contain `logger-rust`; if it does,
the profile gating has been broken.

## Build

```sh
docker build -f Dockerfile.logger-rust -t atf-logger-rust:latest .
```

The image is multi-stage: `rust:1.90-slim-bookworm` + `protobuf-compiler` /
`pkg-config` / `libssl-dev` to build (prost/tonic generate the gRPC code in
`build.rs`), then `debian:bookworm-slim` with only the release binary, running as
the unprivileged `logger` user.

## Run

```sh
docker compose --profile rust-logger up -d logger-rust
```

The service gets its own named volume (`logger-rust-data`) and archive
(`/app/data/atf_logs_rust.db`), so enabling it cannot touch the Python logger's
data. gRPC listens on the crate's documented default `0.0.0.0:50052` *inside its
own container*; that is not a clash with the Python logger, which has its own
network namespace — core reaches whichever one it is pointed at.

## Verify

```sh
docker inspect --format '{{.State.Health.Status}}' atf-logger-rust   # -> healthy
docker logs atf-logger-rust | tail -1        # exactly one "listening on <addr>"
curl -fsS http://127.0.0.1:9109/healthz      # liveness
curl -fsS http://127.0.0.1:9109/readyz       # readiness
curl -fsS http://127.0.0.1:9109/metrics | grep atf_logger_info
docker stop atf-logger-rust                  # graceful drain, exit status 0
docker start atf-logger-rust                 # must recover on the same archive
```

`/readyz` reports *why* it is not ready (for example `storage_unavailable`) —
that reason string is part of the contract, not a decoration.

## Configuration

Every flag and its `ATF_LOGGER_*` environment variable, with defaults, is
tabulated in `rust/logger/README.md`. The engine refuses to start rather than
falling back to a different backend: an unreachable PostgreSQL DSN fails with
`STORAGE_UNAVAILABLE` (an availability problem) as distinct from `STORAGE_ERROR`
(the store answered and the statement failed), and an unsupported DSN scheme is
refused instead of being treated as a SQLite filename.

## Rollback

Disabling the engine is a profile change, not a data migration:

```sh
docker compose --profile rust-logger down logger-rust
```

The Python logger was never stopped, so rollback is complete at that point. Delete
the `logger-rust-data` volume only when its archive is genuinely unwanted.

## Promotion checklist (not yet satisfied)

Before the Rust engine may replace the Python logger:

1. Full suite green: `cargo test --all-targets --no-fail-fast` (lib, gRPC over
   real TCP, spawned-binary, PostgreSQL).
2. `cargo fmt --all --check` and `cargo clippy --all-targets -- -D warnings`
   clean — both are required CI gates.
3. A measured throughput/latency comparison against the Python engine, reported
   from the real benchmark rather than asserted.
4. Core's producer pointed at the Rust service in a staging run, with the
   at-least-once contract observed end to end (no lost records on a Logger
   outage, no record acknowledged before it is durable).
