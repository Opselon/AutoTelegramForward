# Rust Logger engine — deployment runbook

## Status: production

The Rust engine (`rust/logger`) **is** the logging service. The compose service
`logger` builds `Dockerfile.logger-rust`, so `docker compose up -d` starts it and
every consumer keeps using the same address it always has: `logger:50052`.

The retired Python logger (`logger/main.py`, `Dockerfile.logger`) is gone; its
archive (`atf-data` → `/app/data/logger.db`) is **kept untouched** as a
historical record. Nothing writes to it any more, and it must not be deleted:
the Rust engine uses its own store (`logger-rust-data` →
`/app/data/atf_logs_rust.db`).

## Build

```sh
docker compose build logger          # what the deployment does
docker build -f Dockerfile.logger-rust -t atf-logger .   # equivalent, explicit
```

The image is multi-stage: `rust:1.90-slim-bookworm` + `protobuf-compiler` /
`pkg-config` / `libssl-dev` to build (prost/tonic generate the gRPC code in
`build.rs`), then `debian:bookworm-slim` with only the release binary, running as
the unprivileged `logger` user. Only `rust/logger/**` is copied into the build
stage, so a compose/docs change does not invalidate the cached compile.

## Release artifacts

Every supported artifact carries the engine; none of them needs a Rust toolchain
on the target host.

- **Container image** `ghcr.io/<owner>/atf-logger` — built by
  `Dockerfile.logger-rust`; entrypoint is the release binary, healthcheck probes
  `:9109/healthz`.
- **Single-file CLI** (`atf-linux-x64`, `atf-macos-arm64`, `atf-windows-x64.exe`)
  — the release job runs
  `cargo build --release --manifest-path rust/logger/Cargo.toml` and PyInstaller
  bundles `bin/atf-logger` with `--add-binary`. `find_logger()` resolves
  `$ATF_LOGGER_BIN` first, then the frozen bundle (`sys._MEIPASS/atf-logger`),
  then `bin/`, then a cargo release/debug build — and `atf logger` prints the
  exact build command when none of them exists.
- **Source tarball** — ships `rust/logger/`, so a source deployment builds the
  engine from source with that same command.
- **Kubernetes** (`k8s/`) — the `atf-logger` Deployment sets
  `ATF_LOGGER_DB_PATH=/app/data/atf_logs_rust.db` on its own `atf-logger-pvc`
  (never Core's RWO volume), exposes `:50052` gRPC + `:9109` metrics, wires
  liveness `/healthz` and readiness `/readyz` to the metrics port, and allows
  30 s termination grace for the shutdown drain.

```sh
# a release artifact needs no toolchain and no configuration
./atf logger        # gRPC 127.0.0.1:6002, metrics 127.0.0.1:9108, archive data/atf_logs_rust.db
curl -fsS http://127.0.0.1:9108/healthz
```

The release job asserts this before publishing: it starts the frozen binary's
engine, waits for `:9108/healthz`, and fails the release if the bundled engine
does not come up or does not create its SQLite archive. The compose smoke test
(`clean-install`) additionally proves the deployed service is the Rust engine
(no Python runtime in the image, `:9109/metrics` populated) and replays the
24-check gRPC acceptance suite through Core's own stubs.

## Run

```sh
docker compose up -d --no-deps logger
```

gRPC listens on `0.0.0.0:50052`, metrics on `0.0.0.0:9109` (both **inside the
container**; metrics are deliberately not published on the host).

## Verify

```sh
docker inspect --format '{{.State.Health.Status}}' atf-logger       # -> healthy
docker logs atf-logger | tail -2                                    # "listening on ..." + a startup NOTE
docker exec atf-logger curl -fsS http://127.0.0.1:9109/healthz      # liveness
docker exec atf-logger curl -fsS http://127.0.0.1:9109/readyz       # readiness (+ why, if not)
docker exec atf-logger sh -c 'curl -fsS http://127.0.0.1:9109/metrics | head'
docker stop atf-logger      # graceful drain, exit status 0, readiness flips down first
docker start atf-logger     # must recover on the same archive
```

From inside the compose network the acceptance suite is the gate:

```sh
docker exec -i atf-core python - < scripts/acceptance/logger_acceptance.py
```

It exits non-zero on any failed check, so it can be used in operations and CI.

`/readyz` reports *why* it is not ready (for example `storage_unavailable`) —
that reason string is part of the contract, not a decoration.

## Configuration

Every flag and its `ATF_LOGGER_*` environment variable, with defaults, is
tabulated in `rust/logger/README.md`. The engine refuses to start rather than
falling back to a different backend: an unreachable PostgreSQL DSN fails with
`STORAGE_UNAVAILABLE` (an availability problem) as distinct from `STORAGE_ERROR`
(the store answered and the statement failed), and an unsupported DSN scheme is
refused instead of being treated as a SQLite filename.

Collection defaults follow the contract in `docs/logging-contract.md`: WARN,
ERROR and CRITICAL are collected; INFO/SUCCESS/DEBUG/TRACE are not — and the
producers (Core's `LogClient`, the Go API's access logger) have already filtered
them out before the transport, so a verbose level is not even sent by default.

## Cutover acceptance evidence

Recorded on the production host at the cutover (2026-10-16), engine image built
from master `885674f`:

| Check | Command | Result |
| --- | --- | --- |
| Contract suite | `docker exec -i atf-core python - < scripts/acceptance/logger_acceptance.py` | `ACCEPTANCE PASSED` (exit 0) — 34 checks green, 1 skipped when the metrics port is not reachable from the invoking container |
| Redaction keeps ids usable in *both* directions | acceptance checks 8–12 | `chat_id=5094837833` survives as a JSON number and the detail still parses; free text `callback on +155[PHONE] about ticket 42` is masked |
| Correlation ids verbatim + searchable | acceptance check 2 | `stored='acceptance-…'`, lookup by the original id returns the row |
| Secret redaction, unrelated fields intact | acceptance checks 3 | `password`/`access_token` → `[REDACTED]`, `note` kept, `user_id`/`rule_id` columns verbatim |
| Persistence / filter / page / stats | acceptance checks 4 | category, level, search, `limit`/`offset`/`has_more`, `LogStats` all consistent |
| Malformed request reporting | acceptance check 5 | `success=false` `LogRequest.entry is required`, no row written, counted in `events_rejected_total{reason="invalid"}` |
| Core producer queue/pump/drain | acceptance check 6 | `accepted=25 sent=25 failed=0 dropped=0 shutdown_dropped=0`, all 25 persisted |
| Real engine outage (20 s, stopped) | Core's `LogClient` writing throughout | `failed=7 dropped=0`, all 40 records persisted, **Core `/healthz` 200 the whole time** |
| Hard kill (`SIGKILL`, exit 137) mid-burst | engine restarted on the same archive | all 60 distinct records present, `dropped=0`, ready on the first poll |
| Graceful shutdown | `docker stop atf-logger` | `event=shutdown … drained=true remaining=0 steps=[readiness_down, retention_stopped, pipeline_drained, storage_checkpoint, liveness_down]` |
| Live API → engine | authenticated 4xx through `atf-api` | WARN rows with `service="api"`, real `corr-…` ids stored verbatim and queryable |
| Python parity | same payload to the retired Python engine and to Rust | identical redaction/identifier behaviour; only the JSON whitespace of `detail` differs |

**Cleanup is part of the run.** The suite writes every probe under
`service="acceptance"` and purges exactly that scope in a `finally` block, so a
run — green or red — leaves the operator's Logs screen untouched:

```bash
docker exec -e ATF_LOGGER_ADDR=atf-logger:50052 -i atf-core python - \
  < scripts/acceptance/logger_acceptance.py     # prints [cleanup] purged=<n>
```

Before that guard existed, a single run deposited 755 rows into the live store —
most of them ERROR-level, reading `page row 99` — which is what an operator saw
when they opened Logs to ask what was failing. Set
`ATF_ACCEPTANCE_KEEP_ROWS=1` only when you need to inspect the probes afterwards;
delete them yourself (`POST /api/logs/purge {"service":"acceptance"}`).

### Behaviour differences from the retired Python engine (all in the safer direction)

* An entry-less `LogRequest` is **rejected** (`success=false`, nothing written,
  counted); the Python servicer stored an unlabelled `INFO` row.
* An out-of-range page size (`limit <= 0`) is **clamped** to the default page
  (100) instead of a full-table scan.
* `detail` is re-serialized compactly (`{"a":1}` instead of `{"a": 1}`) — the
  parsed content is identical, but a byte-for-byte diff of an old and a new row
  is not meaningful.
* A long digit run inside free text is masked as `[PHONE]` — the same heuristic
  the Python engine applied. Identifiers written into their own columns
  (`correlation_id`, `rule_id`, `user_id`) are **never** masked, so lookups and
  per-task/per-tenant history are unaffected. Query by those columns rather than
  by free-text search when the value contains a long digit run.

## Rollback

The Python logger is no longer deployed, so rollback is a **revision** rollback,
not a data migration:

```sh
git revert <cutover-merge-commit>     # or check out the previous release tag
docker compose build logger && docker compose up -d --no-deps logger
```

Two safety nets are in place, and both were exercised during acceptance:

1. The pre-cutover Python image is retained on the host as
   `autotelegramforward-logger-python:legacy-rollback`; it was started during the
   cutover acceptance run and served the reference comparison in the table above.
2. **Both archives survive.** The Python archive (`atf-data/logger.db`) and the
   Rust archive (`logger-rust-data/atf_logs_rust.db`) are independent volumes;
   neither rollback direction deletes or rewrites the other's data.

Never delete either volume as part of routine cleanup.

## Promotion checklist — satisfied

1. Full suite green: `cargo test --all-targets --no-fail-fast` (lib, gRPC over
   real TCP, spawned-binary, PostgreSQL).
2. `cargo fmt --all --check` and `cargo clippy --all-targets -- -D warnings`
   clean — both are required CI gates, and the `Rust Logger (fmt, clippy, test)`
   check-run is a required check on `master`.
3. Measured throughput/latency comparison against the Python engine — see
   `docs/logger-benchmarks.md` (measured, not asserted).
4. Core's producer pointed at the Rust service in production, with the
   at-least-once contract observed end to end — outage and hard-kill rows above.
