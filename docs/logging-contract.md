# ATF Logging Contract

This document is the authoritative answer to "what does ATF promise about a log
event?" It exists because the previous implementation was ambiguous in a way
that cost real diagnostics: a producer-side transport failure silently destroyed
the records that documented the failure.

Everything here is measured or read off the code in this repository. Where a
number is a claim, the command that reproduces it is quoted next to it. Nothing
here is inferred from "the code is written in Rust".

## 1. Delivery contract (producer → Logger)

`logger/log_client.py` is the producer side. Its counters are the contract:

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

### Acknowledgement semantics (measured on the Python engine)

Read off `logger/main.py` and covered by tests in `logger/tests/test_logger.py`:

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

Reproduce with:

```bash
python -m scripts.bench.logger_baseline --rates 100,1000,10000,50000 --seconds 5 --producers 4
```

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
