# Rust Logger engine — measured benchmarks

Every number below came from a run performed in the session that wrote this
file. Nothing here is estimated, extrapolated or copied from a previous session.
Where a number could not be taken, the gap is named in
[What could not be measured](#what-could-not-be-measured) rather than papered
over.

The harness is `scripts/bench/logger_rust_bench.py`. It replaces
`scripts/bench/logger_baseline.py`, which imported the Python engine
(`from logger.main import LogControlServicer, LoggerDatabase`) and therefore
stopped working when that engine was deleted.

## What the harness measures

It drives the **deployed artifact** — the `autotelegramforward-logger-rust`
container image — over the real gRPC path: real `LogControlService` protobuf
messages, a real SQLite archive on a bind mount, the image's own entrypoint and
default flags. It keeps the four states apart and never lets a client claim
stand in for a durable write:

| State | How it is established |
| --- | --- |
| **OFFERED** | events the producers handed to the transport, paced by a target rate |
| **ACCEPTED** | `Log` answered `success = true` (in `durable` mode: committed to SQLite) |
| **PERSISTED** | `SELECT COUNT(*) FROM logs`, read by the host from the archive **after** the engine stopped |
| **REJECTED** | `Log` failed, or answered `success = false`; the `error_code` is counted, not swallowed |

Requests still in flight when the clock stops are counted as **PENDING** and are
never folded into ACCEPTED.

Isolation: the harness starts its own engine and its own client with
`docker run --rm`, on its own throwaway bridge network, with its own scratch
bind mount under `--scratch`. It never runs `docker compose`, never reuses a
production volume, and never touches `atf-logger-rust`, `atf-logger`,
`atf-core`, `atf-api` or `atf-web`.

## Environment measured

| Fact | Value | How obtained |
| --- | --- | --- |
| Host kernel / arch | `Linux 7.0.0-38-generic x86_64` | `uname -srm` |
| Host CPUs | **2** | `nproc` |
| Docker server | `29.1.3` | `docker version --format '{{.Server.Version}}'` |
| Engine image | `autotelegramforward-logger-rust:latest` | `docker inspect` |
| Engine image id | `sha256:81368e34f6aeac7b9e32ec09c7952d9d53934d0c711a5861915c870c6cef837e` | `docker inspect --format '{{index .Id}}'` |
| Client image | `autotelegramforward-core:latest` | carries grpcio 1.84.0 + the generated stubs |
| Client image id | `sha256:827301cc3b996b797e1c9a0cc0fce42ddebbabcd0a848d48b3a7203aa1448715` | `docker inspect --format '{{index .Id}}'` |
| Host Python (orchestrator) | `3.14.7` | stdlib only — no grpcio needed on the host |

The engine container serves the same wire contract the Python and Go clients
already use (`/autoforward.LogControlService/Log`), so the same generated stubs
drive both.

## How to run it

From the repository root. The orchestrator needs only the `docker` CLI and
Python's stdlib; grpcio and the stubs live in the client container.

```sh
# 1. Prerequisite: the image exists (it is built from Dockerfile.logger-rust).
docker inspect --format '{{index .Id}}' autotelegramforward-logger-rust:latest

# 2. Primary run: full-admission ingest at four offered rates.
python3 -m scripts.bench.logger_rust_bench \
    --rates 1000,10000,50000,100000 --seconds 5 --producers 4 \
    --query-iterations 20 \
    --out /root/.hermes/cache/scratch/logger-rust-bench.json

# 3. Aggregate offered load with four client processes (lifts the harness's
#    own ~10-12k RPC/s per-process ceiling).
python3 -m scripts.bench.logger_rust_bench \
    --rates 100000 --seconds 5 --producers 4 --client-processes 4 \
    --out /root/.hermes/cache/scratch/logger-rust-bench-parallel-client.json

# 4. The engine's contract-default collection policy (WARN/ERROR/CRITICAL only):
#    pass an empty --collect-levels so the harness sets no ATF_LOGGER_* override.
python3 -m scripts.bench.logger_rust_bench \
    --rates 1000 --seconds 5 --producers 4 --collect-levels "" \
    --out /root/.hermes/cache/scratch/logger-rust-bench-default-policy.json

# 5. Keep each run's scratch directory and its database for inspection.
python3 -m scripts.bench.logger_rust_bench --rates 1000 --seconds 5 --keep

# 6. The in-container client half, run by hand against any reachable engine.
python3 -m scripts.bench.logger_rust_bench --client \
    --target-addr 127.0.0.1:50052 --rate 10000 --seconds 5
```

CI-safe unit tests for the harness's pure helpers (no Docker, no engine, no
grpcio required):

```sh
python -m pytest -q scripts/tests/test_logger_rust_bench.py
```

### Defaults the harness pins explicitly

So a bug in the harness cannot silently measure a different configuration than
this document describes, the engine command line always states:

```
--db /app/data/bench.db
--listen 0.0.0.0:50052
--metrics-listen 0.0.0.0:9109
--retention-ts-unit s
--print-addr
--batch-size 512 --batch-delay-ms 2 --ack-mode durable     # from --engine-args
-e ATF_LOGGER_COLLECT_LEVELS=all                           # from --collect-levels
```

`--collect-levels all` is the default because the engine's contract default is
`WARN,ERROR,CRITICAL`: a throughput number taken against the default policy
measures mostly cheap rejections, not ingest. Scenario C below measures the
default policy on purpose, so the difference is visible rather than assumed.

## Results

### Scenario A — ingest, full admission (`ATF_LOGGER_COLLECT_LEVELS=all`)

`--rates 1000,10000,50000,100000 --seconds 5 --producers 4`, profile `small`,
`durable` ack, one client process, image id `sha256:81368e34f6ae…`.

| Target rate | OFFERED | OFFERED/s | ACCEPTED | PERSISTED | REJECTED | PENDING | accepted/s | persisted/s | p50 ms | p95 ms | p99 ms | archive |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 000/s | 5 000 | 999.9 | 5 000 | 5 000 | 0 | 4 | 999.9 | 1 000.0 | 4.541 | 11.339 | 26.946 | 1.3 MB |
| 10 000/s | 49 996 | 9 999.0 | 49 996 | 49 996 | 0 | 34 | 9 999.0 | 9 999.2 | 7.149 | 35.141 | 75.024 | 13.1 MB |
| 50 000/s | 48 129 | 8 553.2 | 48 129 | 48 129 | 0 | 189 | 8 553.2 | 8 548.7 | 15.006 | 73.650 | 125.872 | 12.5 MB |
| 100 000/s | 54 464 | 10 767.1 | 54 464 | 54 464 | 0 | 86 | 10 767.1 | 10 763.6 | 17.692 | 57.537 | 92.587 | 14.2 MB |

Readings:

* **ACCEPTED == PERSISTED == the row count in the archive, at every rate.** In
  `durable` mode `success=true` really does mean "committed": the client's count
  and the host's `SELECT COUNT(*)` agree to the row, and there are zero
  rejections. There is no gap between the ack and the disk.
* At the two upper targets the client could not offer the target rate — the
  whole run had 2 vCPUs to share. The honest number is the achieved offered/s
  (8 553 and 10 767), not the target.
* Latency tracks offered load: p50 grows 4.5 ms → 17.7 ms from 1 000/s to the
  ~10.8k/s plateau; the engine never answered `success=false`, so nothing was
  dropped or shed at these rates.

### Scenario A — read path (archive of 54 464 rows, 14.2 MB)

Measured in the same run as the 100 000/s row above, 20 samples each:

| Probe (gRPC) | p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: |
| `QueryLogs` recent, limit 50 | 0.622 | 0.747 | 3.375 |
| `QueryLogs` `levels=[ERROR,CRITICAL]`, limit 50 | 1.638 | 2.087 | 4.939 |
| `QueryLogs` `search="corr-1234"`, limit 50 | **81.484** | 104.346 | 135.710 |
| `QueryLogs` `rule_id="rule-123"`, limit 50 | 0.295 | 0.503 | 0.987 |
| `QueryLogs` deep page `offset=500`, limit 50 | 0.784 | 0.861 | 1.116 |
| `LogStats` | 12.461 | 18.039 | 55.660 |

The indexed filters (`level`, `rule_id`) and the offset page are sub-millisecond
to low-millisecond. **Free-text `search` is the outlier: p50 ≈ 81 ms against
54k rows**, consistent with a substring scan rather than an index. `LogStats`
aggregation across the archive lands at 12 ms p50. These are single numbers from
one run, not a distribution study.

### Scenario B — aggregate offered load from four client processes

`--rates 100000 --seconds 5 --producers 4 --client-processes 4`:

| OFFERED | ACCEPTED | PERSISTED | REJECTED | PENDING | accepted/s | p50 ms | p95 ms | p99 ms | archive |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 77 488 | 77 488 | 77 488 | 0 | 460 | 15 253.5 | 56.003 | 128.216 | 243.768 | 20.3 MB |

Four processes raise the aggregate offered load from ~10.8k/s to ~15.3k/s, still
with `ACCEPTED == PERSISTED` and zero rejections. The plateau (~12–15k/s across
2/4/8 client processes) is the host, not the engine — see below.

### Scenario C — the engine's contract-default collection policy

`--rates 1000 --seconds 5 --producers 4 --collect-levels ""` (no
`ATF_LOGGER_COLLECT_LEVELS`, so `WARN,ERROR,CRITICAL`):

| OFFERED | ACCEPTED | PERSISTED | REJECTED | accepted/s | p50 ms | p95 ms | p99 ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 5 000 | 1 704 | 1 704 | 3 296 | 340.7 | 3.838 | 5.081 | 8.587 |

Every rejection carries `error_code=FILTERED`, and the harness counts them by
code (`{'rejected:FILTERED': 3296}`). 1 704 of 5 000 are collected — the
producer's level mix is ~34 % `WARN`/`ERROR`/`CRITICAL`, matching the contract's
threshold. Filtering is counted and reported, never silent. This is why
Scenario A sets `all`: a throughput run against the default policy would have
measured rejection cost, not ingest.

### Scenario D — paired side-by-side: Rust engine vs the retired Python engine

The cutover mission could not claim an improvement without measuring the engine
it replaced, so the retired implementation was measured too. The Python engine
is either the retained rollback image
(`autotelegramforward-logger-python:legacy-rollback`) or a historical checkout of
`logger/main.py`; `scripts/bench/logger_legacy_bench.py` drives **either** with
the *same* client container (`autotelegramforward-core`), the *same* flags and
the *same* host, on its own throwaway bridge network with a scratch bind mount —
it never opens the retained `atf-data/logger.db` archive.

```sh
# the Rust half (identical flags to the Python half)
python3 -m scripts.bench.logger_rust_bench --rates 1000,10000 --seconds 5 \
  --producers 4 --profile small --query-iterations 20 \
  --out docs/benchmarks/logger-rust-bench-pairing.json \
  --engine-args --batch-size 512 --batch-delay-ms 2 --ack-mode durable

# the retired Python half
python3 scripts/bench/logger_legacy_bench.py --rates 1000,10000 --seconds 5 \
  --producers 4 --profile small --query-iterations 20 \
  --out docs/benchmarks/logger-python-bench-pairing.json
```

Both halves on the same 2-vCPU host, 4 producer workers, 5 s per rate:

| ENGINE | TARGET/s | OFFERED | ACCEPTED | ACCEPTED/s | REJECTED | ON DISK | p50 ms | p95 ms | p99 ms | producer blocked |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Rust | 1 000 | 5 000 | 5 000 | 999.8 | 0 | 5 000 | 4.5 | 8.9 | 24.8 | 4 ms |
| Python (retired) | 1 000 | 5 000 | 5 000 | 999.8 | 0 | 5 000 | 2.0 | 20.6 | 36.2 | 5 ms |
| Rust | 10 000 | 49 476 | 49 476 | 9 888.6 | 0 | 49 476 | 9.2 | 34.0 | 78.5 | 4.5 s |
| Python (retired) | 10 000 | 7 538 | 7 538 | 1 506.9 | 0 | 7 538 | 154.5 | 289.1 | 295.5 | 19.8 s |

Honest reading of these numbers:

* **At 1 000/s the two are equivalent** (both admit 5 000/5 000 with zero loss),
  and the retired engine's *p50 is lower* — 2.0 ms vs 4.5 ms. That is not
  measurement noise, it is the ack contract: Python commits and answers each
  insert inline, while Rust's `--ack-mode durable` deliberately waits for a
  batch commit, so it pays up to the 2 ms batch delay plus the commit. The Rust
  engine is not faster at this rate, and this document does not claim it is.
* **At 10 000/s the retired engine is the bottleneck**: with 4 producers it
  managed 7 538 of the ~50 000 events it was offered (~1 507/s, p50 154.5 ms)
  and its producers sat blocked 19.8 s of the 20 s of producer wall-clock — the
  client was waiting on the engine, not the other way round. The Rust engine
  accepted every offered event at ~9 889/s with p50 9.2 ms in the same slot.
  **6.6× the admitted load, 17× lower p50, zero rejections on both sides.**
* **The Rust leg is client-limited, not engine-limited** (see "What could not be
  measured"): it hit the harness's own ~10-12k RPC/s per-process ceiling, so
  9 889/s is a floor for the engine and a ceiling for the harness. The Python
  figure is the opposite — a real engine limit, which is why the ratio is
  reported as "≥6.6×", not as a saturation comparison.
* **Read paths are not comparable across legs**: `search_corr` (free-text
  `search` over the archive) took p50 77.1 ms on the Rust leg over 49 476 rows
  vs 2.2 ms on the Python leg over 7 538 rows. Different archive sizes, same
  outlier cause — the Rust engine's free-text search path is the one query with
  a visibly weaker p50, and it is measured honestly in Scenario A as well.
* Both legs recorded `accepted == persisted` at every rate (Rust via the
  engine's own `atf_logger_events_persisted_total`, Python via a host-side
  `SELECT COUNT(*)`), and neither produced a single `OVERLOAD`, `STORAGE`,
  `FILTERED` or error code during these runs.

### Independent cross-check

Three independent sources agree on Scenario A's 1 000/s run:

| Source | Value |
| --- | --- |
| Client gRPC acks (`success=true`) | ACCEPTED = 5 000 |
| Host `SELECT COUNT(*) FROM logs` on the bind mount | PERSISTED = 5 000 |
| Engine `GET /metrics` while it was up | `atf_logger_events_persisted_total 5000` |

The engine's own counters also showed `atf_logger_queue_depth 0` and
`atf_logger_queue_utilization 0.000000` after the drain, and a per-batch size
mostly in the 4–128 bucket — i.e. the batching pipeline, not a per-insert
commit, is what makes the durable ack cheap.

## What could not be measured

Stated plainly, because a missing number is more useful than a guessed one:

* **The engine's saturation point (maximum sustainable events/s)** — not
  measured. On this 2-vCPU host the *client* saturates first: during a run
  `docker stats` showed the client container at 100 % CPU while the engine
  stayed at ~29 % of a core, and the engine answered `success=true` with zero
  rejections at every offered rate including the ~15.3k/s aggregate. The engine
  was never pushed to its limit, so its limit is unknown from these runs.
* **A *saturation* comparison between the two engines** — not measured. The
  paired run in Scenario D drove both engines with identical load, but the Rust
  leg stopped at the harness's own ~10-12k RPC/s per-process ceiling while the
  retired engine stopped at its own limit (~1.5k/s), so the comparison bounds
  the ratio (≥6.6×) instead of locating the Rust engine's true saturation point.
* **Per-engine CPU cost** — not measured. `docker stats` was sampled for the
  Rust runs (client 100 %, engine ~29 %) but no equivalent sample was taken for
  the Python leg, so Scenario D compares throughput and latency only; no
  efficiency-per-event claim is made.
* **The PostgreSQL backend** — not measured. The harness drives the SQLite
  archive under a bind mount; it has no DSN option.
* **Sustained-load behaviour** — not measured. Every run here is a 5 s burst;
  multi-minute soak, retention-worker interference under load, and restart
  recovery under load were out of scope.
* **Behaviour past the host's CPU limit** (overload/`OVERLOAD` shedding,
  queue-full latency) — not measured, precisely because the engine was never
  saturated.

## Where the results landed

* **Numbers and commands:** this file.
* **Raw JSON reports** (full per-run detail, including the exact engine and
  client command lines, the listening address, the metrics excerpt and the
  engine log tail), committed next to this document:
  * `docs/benchmarks/logger-rust-bench-scenario-a.json` — Scenario A
  * `docs/benchmarks/logger-rust-bench-scenario-b.json` — Scenario B
  * `docs/benchmarks/logger-rust-bench-scenario-c.json` — Scenario C
  * `docs/benchmarks/logger-rust-bench-pairing.json` — Scenario D, Rust leg
  * `docs/benchmarks/logger-python-bench-pairing.json` — Scenario D, retired
    Python engine leg (same client, same flags)
* **Harnesses:** `scripts/bench/logger_rust_bench.py` (either engine; the `logger`
  service name is unchanged) and `scripts/bench/logger_legacy_bench.py` (the
  retired Python engine, from its rollback image or a historical checkout).
* **Unit tests:** `scripts/tests/test_logger_rust_bench.py` (10 tests, no Docker
  required). Full suite after the change:
  `python -m pytest -q core/tests scripts/tests` → `821 passed, 9 skipped`.
