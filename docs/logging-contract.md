# ATF Logging Contract

This document is the authoritative answer to "what does ATF promise about a log
event?" It exists because the previous implementation was ambiguous in a way
that cost real diagnostics: a producer-side transport failure silently destroyed
the records that documented the failure.

Everything here is measured or read off the code in this repository. Where a
number is a claim, the command that reproduces it is quoted next to it. Nothing
here is inferred from "the code is written in Rust".

## 1. Delivery contract (producer → Logger)

`core/observability/log_client.py` is the producer side. Its counters are the contract:

| counter | meaning |
| --- | --- |
| `accepted` | admitted to this process's bounded in-memory queue. **Not durable yet.** |
| `sent` | the Logger answered `success=true` for this record. |
| `failed` | an RPC attempt errored. The record is **kept and retried** — not lost. |
| `dropped` | the queue was full and the **oldest** queued record was evicted (bounded memory: keep the newest context). |
| `disabled` | the client was switched off and refused the record. |
| `shutdown_dropped` | still undelivered when the process stopped, after a bounded drain. |
| `pending` | `queue depth + in-flight` — records admitted but not acknowledged. |

Guarantees:

* **At-least-once, never at-most-once.** A timeout can duplicate a record the
  Logger already stored. Suppressing duplicates needs a stable event id plus a
  unique index in the store — a schema/contract change that is deliberately not
  faked. Consumers must tolerate duplicates.
* **No silent loss.** A record leaves the queue only when `sent` increments, or
  when it is counted in `dropped` / `shutdown_dropped` and logged loudly.
* **`accepted - sent` is the crash-exposure window.** A process that dies with
  unsent records loses them; that number is the size of the loss, and it is
  visible while the process runs.
* **`failed` is a failure count, not a loss count.** Conflating the two is how a
  Logger outage used to look like a healthy producer.
* **A Logger outage must never block forwarding.** `log()` never raises into
  caller business logic, and never awaits the network.
* **What `sent` does *not* promise:** whether the Logger has committed the record
  to durable storage or only accepted it into its own queue is the **Logger's**
  contract, documented in §4. Until that is ack-gated, "accepted by Logger" must
  not be described as "durable".

## 2. Severity levels and defaults

Canonical levels, ordered: `TRACE`, `DEBUG`, `INFO`, `SUCCESS`, `WARN`,
`ERROR`, `CRITICAL`.

* `WARNING` and `WARN` are the same level; `WARNING` is accepted as a legacy
  alias and normalized to `WARN`. Nothing in ATF compares level strings
  literally, so a stored `WARNING` from an older archive stays readable.
* Unknown level strings are preserved as-is for reading and treated as `INFO`
  for thresholding (never silently upgraded to a security level).

Default collection policy for a **new or migrated installation**:

| level | collected by default | notes |
| --- | --- | --- |
| `CRITICAL` | yes | never suppressible by ordinary filtering |
| `ERROR` | yes | |
| `WARN` | yes | |
| `INFO` | **no** | enable explicitly (admin switch or time-limited diagnostic mode) |
| `SUCCESS` | **no** | high-volume happy path; use counters/histograms/sampling instead |
| `DEBUG` | **no** | **requires explicit enablement**; implies a time limit in the admin UI |
| `TRACE` | **no** | **requires explicit enablement** and is the first thing a diagnostic mode turns off |

Two rules override the table:

* **Security-relevant audit events are not disabled by a severity threshold.**
  Elevating the threshold to shut up chatty `INFO` noise must not disarm
  authorization-failure, cross-tenant-attempt or admin-change auditing. Those
  events are collected because of what they *are*, not because they are `WARN`.
* **Emergency logging is unconditional.** Startup, shutdown, fatal errors and
  Logger self-health diagnostics travel a minimal, non-recursive path (stderr
  and the health endpoint) that does not depend on the configured severity,
  the configured storage backend, or the main logging pipeline. A Logger that
  cannot write its own logs still reports why.

Migrated installations get exactly this table — not "whatever the old config
said" — because the old behaviour (`INFO` collected by default) is what made the
hot path expensive.

### Where the default policy is enforced today

The table above is a *policy*, and a policy only exists if something enforces it.
The Logger stores whatever it receives (it has no server-side severity filter), so
the defaults are enforced **at each producer**. Two services produce events, and
both apply the same policy:

**Python Core** — `core/application/log_policy.py`, attached to the **`LogClient`**
— the door every event in a process leaves through, since the audit choke point,
the `info/warn/error` helpers and the stdlib logging bridge all funnel into it. One
decision at that door therefore covers every emission path; the pipeline's
`_audit()` helper gates nothing itself, it *names* its events so a
security-relevant one can be exempted:

* `ATF_LOG_LEVELS` — the collected set (default `WARN,ERROR,CRITICAL`).
* `ATF_LOG_DIAGNOSTIC_SECONDS` — start a bounded diagnostic window at launch.
* `ATF_LOG_VERBOSE_RATE` — a per-second cap on verbose events (0 = uncapped), so
  turning diagnostics on under load cannot itself become the incident.

**Go API** — `api/internal/logpolicy`, applied in `middleware.go` before the
transport, on both emission paths (`AccessLogger` records every request;
`ErrorLogger` the failures only). Both are covered because an exemption applied to
one of them is not an exemption: the other one keeps writing the rows the contract
says must not be collected. The middleware classifies each request from its
**method, route namespace and status code** — never from the rendered log text,
which would silently change what is audited the next time a message is reworded —
and `main.go` attaches the policy with the same environment contract as Core.

The two implementations are held to **one** set of expectations:
`contracts/log_policy_decisions.json` is evaluated by
`core/tests/test_log_policy_fixtures.py` *and*
`api/internal/logpolicy/policy_test.go`. Two hand-written implementations of one
rule drift apart precisely when nobody is looking; the fixture is what notices. It
earned its keep immediately: the first run caught the Go level-list parser
downgrading an unknown name to `INFO` while Python dropped it — a typo in
`ATF_LOG_LEVELS` would have quietly re-enabled a verbose level in one service and
not the other.

### Who owns the effective policy (and what is still missing)

Today **no process owns it**: each producer reads its own environment at
construction, so there is no persisted policy, no version, no propagation to a
running process and no staleness detection. A change means an environment update
and a restart of the affected service. That is a limitation of the current state,
not a design — the intended owner is the Rust Logger (persisted, versioned,
published), with producers caching the last validated version and reporting its age
so a stale cache is visible rather than silently effective. Until that exists,
nothing may describe the policy as centrally managed.

`LogPolicy.snapshot()` consequently exposes only **process-local** counters: a
number it reports is what *that* process decided, never a system total.

### Control plane (Go API)

| Route | Auth | Behaviour |
| --- | --- | --- |
| `GET /api/logs/policy` | any authenticated caller | Effective policy per producer, each scope labelled `api` / `core`, with the scope's counters and an explicit `counter_scope` string |
| `GET /api/logs/health` | any authenticated caller | Per-service logging reachability plus record age, so "quiet" can be told apart from "not being fed" |
| `POST /api/logs/diagnostics/start` | **admin only** | Opens one bounded window (default 600 s, hard maximum 3600 s) on this API's producer policy |
| `POST /api/logs/diagnostics/stop` | **admin only** | Closes it early |

Four properties the routes are built around, each with a test:

1. **A scope that cannot be read is reported unavailable, not zero.** `atf-core`
   enforces the same policy in its own process and exposes no RPC for it yet, so
   it is returned with `available: false` and a reason. Reporting `0 suppressed`
   there would read as "nothing was suppressed" — the opposite of the truth, and
   the kind of number an operator would act on.
2. **A start always yields exactly one window.** It replaces an active one and
   says so (`replaced_active`), and a second start within 5 s is refused with
   `409` + `Retry-After`, so a double-clicked button cannot hold collection open
   indefinitely. A refused press is not counted as an activation.
3. **Invalid input is refused, never silently coerced.** A duration outside
   `1..3600` returns `400` with the accepted range.
4. **A policy change cannot suppress the evidence of itself.** Mutations write a
   durable `admin.change` record through the *same* policy they change;
   `admin.change` is security-relevant, so a window narrowed to `CRITICAL` still
   records who narrowed it. Removing that exemption fails
   `TestAdminAuditSurvivesAMaximallyRestrictivePolicy`.

Reads are authenticated but not admin-only: the answer describes *this* API
process's own configuration, not another tenant's data. Mutations are admin-only
via the existing server-verified `RequireAdmin`, which reads the identity Core
validated — never a client-supplied `is_admin`.

Two rules the producer must not get wrong, and both are tested:

1. **The gate governs the log emission only.** The durable audit write shares no
   gate with it: suppressing a noisy category must never erase the audit trail.
   The first version of this code returned from the choke point on suppression
   and silently dropped the audit record with it — caught by
   `test_suppressing_log_verbosity_never_erases_the_durable_audit_trail` — and the
   second put the gate at one call site, which a partially-covered policy is.
2. **Suppression is not a queue drop.** A record the policy refuses is counted by
   the policy (`suppressed_level`, `suppressed_rate`); it never appears in the
   client's `dropped` counter, which means "bounded-queue eviction" and nothing
   else. An operator judging volume must be able to tell "we chose not to collect
   this" apart from "we lost it".
3. **Suppression is counted, never silent.** `LogPolicy.snapshot()` reports
   `emitted` / `emitted_security` / `suppressed_level` / `suppressed_rate` and the
   effective level set, so "collection is off" is distinguishable from "nothing
   happened". Security-relevant events (`auth.*`, `tenant.*`, `admin.*`,
   `UNAUTHORIZED`, `CROSS_TENANT`, …) bypass the threshold, the diagnostic state
   and the rate limit.

Once the Rust engine owns a server-side filter, that filter becomes the primary
enforcement point and this producer-side policy stays as defence in depth.

## 3. Four separate concepts that must not be conflated

1. **Collection** — does the producer/Logger take the event at all? (§2)
2. **Persistence** — has it reached durable storage? (ack semantics)
3. **Query visibility** — can an administrator see it through the API/UI? A
   record can be persisted and still be invisible to a tenant who is not
   authorized for it.
4. **Retention** — how long is it kept? **Disabling a category never deletes
   previously stored records.** Turning off `DEBUG` collection stops new
   `DEBUG` events; it is not a purge. Purging is a separate, audited,
   explicitly-scoped operation (§5).

Confusing (1) with (4) is how administrators lose history they thought they
still had.

## 4. Storage backends

Both SQLite and PostgreSQL are supported first-class; the choice is explicit
validated configuration, and **Logger never silently falls back** to a different
backend after a startup failure — it fails loudly instead, because a Logger
quietly writing to the wrong database is worse than a Logger that will not start.

* **SQLite** — WAL where safe, bounded `busy_timeout`, batched transactions in a
  single writer path, no aggressive checkpoint/vacuum on the hot path, archive
  and migration compatibility with existing deployments.
* **PostgreSQL** — bounded pool with acquisition/statement/transaction timeouts,
  batched inserts, bounded ingestion parallelism, pool-saturation and slow-query
  observability, retries that do not duplicate records.

Moving SQLite → PostgreSQL is an explicit, documented export/import procedure
with row-count and checksum verification — never an unbounded automatic
migration during normal startup. Existing SQLite data is never silently
discarded.

### Acknowledgement semantics (inherited from the retired Python engine, kept by the Rust engine)

Read off the retired `logger/main.py` (now `rust/logger/src/ingest.rs` +
`storage*.rs`, covered by the crate's own tests and by
`scripts/acceptance/logger_acceptance.py`):

* `insert()` commits **before** the RPC answers `success=true`, so the
  producer's `sent` means *committed*, not *queued*.
* `journal_mode=WAL` with `synchronous=FULL` (written explicitly now, not
  inherited from a default) ⇒ that commit is fsynced into the WAL, so an
  acknowledged record survives a power loss. This is what makes `sent`
  trustworthy rather than aspirational.
* `busy_timeout` is now **5000 ms** (`ATF_LOGGER_BUSY_TIMEOUT_MS`). It used to be
  SQLite's default of **0**, which turns ordinary write contention into an
  immediate `database is locked` failure — a log entry discarded for no reason.
  An unparseable value falls back to the default instead of taking logging down.
* `wal_checkpoint(PASSIVE)` runs only on the purge path, never on the ingest hot
  path.
* Write concurrency is **one connection behind one lock**; batching, not more
  writers, is what raises SQLite throughput. The measured consequence is the
  ~1,150 events/s ceiling in §5.

## 5. Measured baseline (Python Logger engine, before migration)

This baseline was produced by `scripts/bench/logger_baseline.py`, which imported
the Python engine directly (`from logger.main import …`) and was removed with it.
The historical numbers below were **not** re-confirmed against the Rust engine.

```bash
# the old command (the script no longer exists):
# python -m scripts.bench.logger_baseline --rates 100,1000,10000,50000 --seconds 5 --producers 4
```

The current harness for the Rust engine is `scripts/bench/logger_rust_bench.py`;
its measured results and exact commands are in
[`docs/logger-benchmarks.md`](logger-benchmarks.md).

Measured on this repository's Python logger (SQLite, one connection behind a
lock, commit per insert, no admission control):

* saturation ≈ **1,150 events/s**
* p50 ingest latency ≈ **215 ms** under load
* no admission control: offered load above saturation turns into queue growth
  and latency, not into a rejected-and-counted signal

The benchmark reports OFFERED / ACCEPTED / PERSISTED / REJECTED / PENDING
separately and verifies the rate against real row counts in the database, so a
"good" number cannot come from the client lying about delivery.

**This is not a claim of "million-scale ready".** That designation requires a
defined target workload, measured throughput and p95/p99 latency on both
backends, tested failure recovery, resource limits and demonstrated database
capacity. The numbers above are a *baseline to beat*, not a result.
