# Logger Service: Architecture Audit & Modernization Plan

Scope: `rust/logger` in `Opselon/AutoTelegramForward` — the Rust gRPC logging
service that stores the observability records written by every Python and Go
service in this repository.

This document is the audit the work is built on. Sections:
**1. Current state** (what exists and is verified), **2. Real gaps** (ranked,
with evidence), **3. Deliberately not built** and why, **4. The plan** (small PRs),
**5. Benchmark baseline & results**, **6. Status.**

Findings cite symbols (not line numbers, so this document does not rot), and
every claim was checked against the code, the protobuf schemas and the running
deployment.

---

## 1. Current state — what the service already has (verified)

Inventory of capabilities that **already exist** and are therefore *not*
re-implemented by this effort:

### Ingestion path

- **Non-blocking ingest with bounded lanes**: `transport.rs` `Log`/`LogBatch`
  hand records to `pipeline.rs`, where each severity class owns a fixed-capacity
  lane (`VecDeque`); an over-capacity submit is refused and counted
  (`queue_rejected_full`{lane}), never buffered "just this once". The storage
  loop drains out-of-band, so the ingest call never waits on SQLite/Postgres.
  Tests: `overflow_is_refused_and_counted_not_buffered`,
  `priority_lane_is_drained_first`,
  `priority_lane_drains_first_and_each_lane_keeps_fifo_order`,
  `concurrent_producers_never_lose_or_double_count_a_record`.
- **Ack modes that match reality**: `AckMode::Queued` acknowledges once inside
  the bounded queue; `AckMode::Durable` acknowledges only after the batch
  commits (`durable_ack_means_committed`,
  `concurrent_durable_acks_agree_with_the_rows_on_disk`); queued mode reports a
  *degraded* readiness instead of a green one (`queued_ack_mode_is_degraded_...`,
  `queued_ack_mode_reports_degraded_readiness`).
- **Producer-visible storage failures**: a failed storage write is reported back
  and counted (`storage_failure_is_reported_to_the_producer_and_counted`), and a
  refused record stays retryable without becoming a duplicate
  (`a_refused_record_stays_retryable_and_is_not_a_duplicate`).
- **Bounded payload size**: `ingest::Limits` + `transport` validation reject an
  oversized submission atomically (`max_bytes_overflow_is_rejected_with_a_status`
  family; `oversized_batch_is_rejected_atomically`); `prepare` bounds and
  redacts every field before the record is queued.
- **Payload hardening**: `ingest::redaction` (byte-parity fixtures with the
  Python engine's `redact`) masks credentials in `message`, `detail` and the
  structured identifiers (`rule_id`, `correlation_id`, and from PR-1 on:
  `request_id`, `trace_id`) — anchored patterns for `Authorization`/`Bearer`
  headers, JWTs, `token=`/`key=`/`session=` pairs, bot-token shapes, phone
  numbers (`bearer_tokens_are_redacted_and_scoped`,
  `ingest_path_closes_the_auth_scheme_hole`, `phones_keep_four_characters`,
  `detail_redaction_*`, `bot_tokens_sessions_and_hashes_are_redacted`).
- **Dedupe with a bounded window**: `dedupe_key` over the record shape
  (`dedupe_key_is_stable_and_field_sensitive`,
  `dedupe_window_suppresses_and_counts_duplicates`) — duplicate payloads inside
  the window are suppressed *and counted*, not stored twice.
- **Log policy (producer-side, dynamic)**: `core/application/log_policy.py`
  implements a collect-default of CRITICAL/ERROR/WARN, a **time-limited
  diagnostic mode** that enables the verbose levels and self-expires, a
  **verbose-only rate limit** (`verbose_rate_per_second`; WARN/ERROR storms are
  never throttled), and a **security exemption** (auth/tenant/permission events
  are never suppressed) — mutated in-band through the Go API
  (`api/internal/handlers/logging_policy.go`,
  `core/tests/test_log_policy*.py`). The Rust engine stores whatever arrives;
  the policy lives where events are produced (this split is documented in the
  engine itself and kept by this effort).
- **No seq-sampling, no engine-side rate limit**: verified — no sampling and no
  admission rate limiter exists anywhere today (engine or client). See GAP-4.
- **Overload refusal, not storm absorption**: a saturated lane refuses with
  `OVERLOAD` and the refusal is counted (`overflow_is_refused_and_counted_not_buffered`,
  `overload_is_refused_with_overload_and_counted`) — the producer's bounded
  queue absorbs bursts, so backpressure is real rather than silently queued.

### Storage

- **Backends**: `sqlite.rs` (WAL, default) and `postgres.rs`; chosen by
  `ATF_LOGGER_BACKEND`.
- **Migrations**: additive, gated by `PRAGMA user_version` and asserted to
  contain only `ADD COLUMN`/`CREATE INDEX` statements
  (`migrations_are_idempotent_statements`), plus a *proven* legacy path
  (`migration_reads_an_existing_python_archive`; and with PR-1:
  `an_old_archive_is_migrated_in_place_with_the_structured_context_columns`)
  that opens a Python-built v18 archive and migrates it in place.
- **Indexes**: `idx_logs_{ts,level,category,service,rule,user,corr}` on the
  equality/range columns the pager and stats use (+ PR-1: `idx_logs_trace`,
  `idx_logs_fingerprint`).
- **Time-range paging**: `query.rs` `Page` (`since`/`until`, clamped `limit`,
  `ascending`) with a **keyset seek** (`keyset_seek_replaces_offset`), equality
  filters on service/category/level/rule/correlation + bounds
  (`query_logs_mirrors_the_python_filter_semantics`).
- **Retention**: bounded, audit-protected purge with WAL checkpointing
  (`purge_is_bounded_protects_audit_and_checkpoints`,
  `cycle_purges_old_rows_and_keeps_protected_audit_rows`,
  `budget_bounds_one_cycle_and_reports_leftovers`), plus purge RPC
  `purge_logs_respects_time_scope_and_protected_categories`.
- **Fsync / durability**: SQLite `synchronous=FULL` + WAL (`pragma_*` tests).

### Query surface

- `QueryLogs` (paged filters), `LogStats` (by level/category/rule/service),
  `PurgeLogs`, `DeleteUserLogs` (tenant erasure, `delete_user_logs_is_exact*`
  tests), all with per-RPC guard.

### Operations

- **Metrics**: `metrics.rs` — ingest accepted/refused (per method and
  per-service plateaus), queue per tier + per-service lane depth, dedupe /
  sampling / summary, storage transaction failures, purges, latency (P50/P95/
  P99 via histogram), RPC counts by status.
- **Health/readiness**: `health.rs` — `liveness` (process up) vs `readyz`
  (storage verified). Readiness flips off if storage fails and recovers
  (`readiness_recovers_after_storage_failure`).
- **Auth: absent (see GAP-3)** — verified during this audit: no intercept, no
  token, no credential of any kind on the gRPC service. Every RPC (ingest, read,
  purge, delete-user-logs) is accepted from any peer that can reach the port.
  The consumers (`core/observability/log_client.py`, the Go API via
  `grpcclient.DialWithoutCredential` — the "different trust domain" comment at
  that symbol) deliberately send nothing today because there is nothing to
  check against.

### Producer (Python) path

- `core/observability/log_client.py` — batching (`batch_max`, 1s flush timer,
  128-entry bounded queue, overflow drops lowest severities first) + retry with
  damped backoff, refusing to hold the caller (fire-and-forget semantics,
  `should_refuse_would_refuse`)
- Emit sites carry `rule_id`, `user_id`, `correlation_id` on all call sites.

## 2. Real gaps (ranked, with the evidence)

The audit found **5 real, verifiable gaps**. Anything else in the request list
was verified to already exist (Section 1) and was not re-built.

### GAP-1 — Structured context is only one identifier (severity: high)

`Record` carries `rule_id`, `user_id`, `correlation_id` but nothing for HTTP/
grpc request ids, W3C `trace_id` or a stable `module` (code unit). Distributed
debugging today means free-text search over `message`.

**Fix (PR-1, this document §4)**: additive schema columns
`request_id`, `trace_id`, `module` (`TEXT NOT NULL DEFAULT ''` in SQLite /
`ADD COLUMN IF NOT EXISTS` in Postgres) + equality indexes, mirrored
proto fields, `QueryLogs` filters, producer writes the fields on
emit (Python `log()` extension).

### GAP-2 — Recurring failures are not grouped (severity: high)

There is no **error fingerprinting**: two identical failures with different
user ids / timestamps are two unrelated rows. `core`'s "Error in forwarding"
messages collapse thousands of rows; the panel has no notion of "one recurring
error".

**Fix (PR-1)**: engine-computed **fingerprint** at ingest
(FNV-1a over the *redacted* record shape: `category|module|
message-with-producer-numbers-normalised` *without* level and *without*
service — severity and the reporting service are *reported* from the newest
occurrence, matching the engine's already-shipped aggregate contract that a
failure firing at WARN and later at ERROR, or from another service, is ONE
recurring group), replacing unstable ids before
hashing so the signature does not change per user/timestamp/secret; stored
read-only (accept from the wire is **never** done — an ingest-supplied
fingerprint is ignored), enabled via `QueryLogs` and `LogStats.include_errors`,
exposed as `LogStatsResponse.error_groups` (new `ErrorGroup` message).

### GAP-3 — No authentication on the LogService surface (severity: critical)

Verified during this audit:

- `rust/logger/src/transport.rs` has **no intercept and no credential check**:
  every RPC — ingest (`Log`/`LogBatch`), read (`QueryLogs`, `LogStats`),
  administrative (`PurgeLogs`, `DeleteUserLogs`) — is served for any peer that
  can open a connection, over an `insecure` channel.
- No producer sends a credential either: the Python client connects insecurely
  with no metadata, and the Go API dials the logger through
  `grpcclient::DialWithoutCredential` (its comment documents the logger as a
  "different trust domain" with no use for the core credential).
- Consequently the archive, purge and tenant-erasure RPCs are one lost network
  boundary away from modification or deletion by any container/host process.
  The deployment currently mitigates this only by keeping the listener inside
  the compose network.

**Fix (PR-2) — DONE in `feat/logger-auth`**: real authentication, additive:

- `ATF_LOGGER_INGEST_TOKEN`, `ATF_LOGGER_READ_TOKEN`, `ATF_LOGGER_ADMIN_TOKEN`
  (one secret per capability class, never one master key); unset token = scope
  open (current behavior preserved, so an unattended rotation cannot lock
  producers out; compose documentation records this trade-off).
- Server-side constant-time compare on `authorization: Bearer ...` metadata;
  ingest token only reaches `Log`/`LogBatch`, read token only
  `QueryLogs`/`LogStats`, admin token only `PurgeLogs`/`DeleteUserLogs` — a
  leaked producer token cannot read the archive.
- Producers updated: the Python client and the Go API read their own
  logger-token variable and attach it in the outgoing metadata.
- `metrics.rs`: `atf_logger_auth_failures_total{outcome}` so "someone reached
  the port unauthenticated" is a first-class operational signal — `(scope ×
  missing|malformed|mismatch|forbidden)` folded into one bounded label, 12
  possible series, never a raw token.
- Enforced *inside* the four RPC methods (Log = ingest, QueryLogs/LogStats =
  read, PurgeLogs = admin) as `RpcGuard`-counted entry: the failure is
  UNAUTHENTICATED (16) for absent/unknown credentials and PERMISSION_DENIED (7)
  for a credential belonging to another capability class, and it is recorded in
  `rpc_total`/`rpc_status_total` exactly like every other rejection.
- Deleted `LogBatch`/`DeleteUserLogs` naming confusion: the proto serves
  exactly `Log`, `QueryLogs`, `LogStats`, `PurgeLogs` (Section 1), so the PR-2
  plan's `LogBatch`/`DeleteUserLogs` classes collapse into `Log` and don't
  exist as separate RPCs.
- Method classification defaults everything unknown to **admin** (deny for all
  existing tokens): an RPC added to the proto later can never inherit an open
  scope by accident.

### GAP-4 — No engine-side admission limiter; rate limiting only protects *enabled* verbose events (severity: medium)

The producer-side policy (`core/application/log_policy.py`) rate-limits the
verbose levels and exempts security events, and the engine dedupes repeated
payloads inside a window. What remains genuinely missing:

- The engine stores **everything an *authenticated-by-network* peer sends**:
  there is no per-service admission budget on the Rust side, so a runaway or
  malicious producer (once GAP-3 is fixed, once it holds a valid ingest token)
  can still saturate the boundless happy path of `WARN+` records (dedupe
  collapses only identical payloads inside the window, not chatty-but-distinct
  lines).
- No **sampling** knob exists anywhere: an INFO-storm under diagnostics is
  limited by `verbose_rate_per_second` on the producer, but the engine himself
  has no second line of defense.

**Fix (PR-3)**, deliberately narrow because the producer policy already carries
most of the requirement:

- Per-service **admission budget** on the engine (token bucket keyed by the
  authenticated service identity, once PR-2's tokens exist), generous by
  default (`ATF_LOGGER_MAX_RECORDS_PER_SERVICE_PER_SECOND`), **CRITICAL always
  admitted** (a noise limiter must never disarm the audit trail, mirroring the
  Python policy's security exemption).
- Engine-side **sampling** only for verbose levels when a service exceeds its
  `INFO-window quota` — never for WARN/ERROR/CRITICAL (same principle).
- Both counters exported (`rate_limited_total{service}`,`sampled_total{service}`)
  so "the limiter did something" is visible and the producer-visible refusal
  contract stays honest.

### GAP-5 — Operational failure edges: disk-full has no distinct voice (severity: medium)

What already exists (verified): bounded, audit-protected purge with a WAL
checkpoint after each cycle (`purge_is_bounded_protects_audit_and_checkpoints`;
`wal_checkpoint(PASSIVE)` post-purge, `TRUNCATE` on close), a `StorageError`
taxonomy (Busy/Transient/Backend/Unavailable/Config with `is_retryable`), and readiness
flipping off on storage failure (`a_stalled_storage_is_reported_not_hidden`).

What remains genuinely missing:

- `SQLITE_FULL` (disk full) is *not* in `map_error`'s classification, so it
  falls into the generic `Backend` bucket — indistinguishable, for metrics and
  for the producer's retry answer, from a broken statement. A full disk is a
  retryable environmental condition, not a code bug.
- `metrics.rs` exposes failures as counters but not by **kind**, and has no WAL
  size gauge, so an operator sees "storage failures" without "why / how close
  to full".

**Fix (PR-3)** (bounded):
- map `SQLITE_FULL`/`SQLITE_IOERR_WRITE` to a retryable class
  (`StorageError::Transient`), documented.
- `storage_failures_total{source}` labeled counter + `wal_size_bytes` gauge;
  ingest keeps refusing when full and recovers when space returns — proven by a
  forced disk-full test (SQLite `max_page_count` sandbox).

## 3. Deliberately not built (and why)

| Proposal | Decision | Reason |
| --- | --- | --- |
| OpenTelemetry / OTLP exporter | **post-1.0, config-gated** | `opentelemetry-otlp` pulls ~40 crates and a runtime collector dependency; the concrete consumption path today is Grafana/Prometheus + the panel, not an OTLP collector. Will be revisited when a first consumer exists; the schema is already designed so `trace_id`/`request_id` map 1:1 to OTLP (W3C format). |
| FTS5 / trigram full-text index | **not now** | `search` is a bounded `LIKE '%..%'` today (perf audit pending on real archive sizes ≥ millions of rows); FTS5 would double storage for every line. Decision deferred until the benchmark numbers from §5 say it is needed. |
| Engine-side WAL / bounded queue against storage failures | **not now** | Ingest already runs on a bounded RAM queue and refuses rather than buffering unboundedly; a second persistent staging layer (embedded KV/WAL) trades operational complexity + a loss-mode for marginal durability gain given the **producer-side** queue is already bounded-and-retry (see §1 producers). Recorded as a future option if real measurements show loss-mode hurt. |
| JSON column per record | **not now** | The archive already stores 1:1 columns for everything producers send; a parallel JSON blob would double rows for little relevant query power and break the SQL parity tests. |
| TLS on the logger gRPC listener | **not now** | Deployment is pod-network (docker-compose `logger` internal); TLS adds a cert-rotation surface in front of a trusted-network listener. Documented in the audit: if the listener ever leaves the pod network, TLS becomes a hard prereq (recorded as a follow-up issue). |
| Go ownership of the logger | **rejected** | The Rust logger is already the parity-tested, benchmarked, fuzzed part of the system with the stability longest. Moving ownership between languages has no measurable basis; the audit explicitly keeps it. |
| Per-tenant database shuffle | **rejected** | Tenant data is single-database with `user_id` columns + indexes + `DeleteUserLogs`; sharding would complicate the migration file and remove already-tested code paths. |

## 4. The plan (small PRs, in order)

| # | PR | Content | Additive only |
| --- | --- | --- | --- |
| PR-1 | Structured context + fingerprinting + error groups | §2 GAP-1+GAP-2: new schema v19 columns; `Record`/`Row` grows `request_id`/`trace_id`/`module`/`fingerprint`; indexer `idx_logs_trace`/`idx_logs_fingerprint`; `QueryFilter` grows the 3 fields (exact, not substring); `LogStatsRequest.include_errors = 3` + `ErrorGroup`; ingest fingerprinting (`ingest.fingerprint_of`) + transport surfaces it; legacy archives migrate in place with all fields defaulting `""`; Python producer fills `request_id`, `trace_id`, `module` on emit; regression tests in layers | Yes — new fields and RPC parameters, no schema break |
| PR-2 | Service-surface authentication | §2 GAP-3: intercept becomes a real token check (ingest/read/admin config); Python + Go send their token; metrics `auth_failures_total`; tests for missing/incorrect/user-mismatched tokens, per-path minimum | No token config default = current behavior (docs: config entry, no breaking change) |
| PR-3 | Ingest protection + storage-failure observability | §2 GAP-4+GAP-5: per-service rate limiting, low-severity sampling; storage-failure classification metric; disk-full sandbox test; WAL checkpoint gauges | Yes |
| PR-4 | Benchmark + final report | §5 numbers before/after with the bench harness; docs | — |

Every PR carries its own test chunk; docs (`docs/logger-modernization-baseline.md`,
`docs/logger-benchmarks.md`) grow a section per gap instead of being rewritten.

## 5. Benchmark baseline & results

The benchharness `scripts/bench/logger_rust_bench.py` is the before/after
instrument (same artifact, same box, same builder):

<!-- BENCH_TABLE: DO NOT EDIT BY HAND — filled from scripts/bench runs -->

Scenario | Baseline | After PR-1 | After PR-3
--- | --- | --- | ---
10k-record ingest (client→logger) seq | *pending run* | | 
10k-record ingest burst (concurrency 1 / 8) | *pending run* |
100k-record QueryLogs filtered page | *pending run* |

Numbers are added in place as the PRs land.

## 6. Status

| PR | Area | Status |
| --- | --- | --- |
| PR-1 | Structured context + fingerprint + error groups | in progress |
| PR-2 | Service-surface authentication | pending |
| PR-3 | Ingest protection + storage observability | pending |
| PR-4 | Final benchmark report | pending |
