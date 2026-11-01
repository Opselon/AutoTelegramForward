# Data Ownership

> Stage 1 of the Go Worker migration adds **no tables, no columns, no
> indexes and no migrations**. It changes *who reads and writes* existing
> tables, and only under an opt-in flag.

## The existing stores

| Store | Production | Local / test | Selected by |
|---|---|---|---|
| PostgreSQL | the primary deployment model | — | `ATF_DATABASE_URL` |
| SQLite (`data/atf.db`) | supported local/single-host deployments | every test suite | `ATF_DB_PATH` |

Both are created and migrated by the Python Core's own
`core/infrastructure/persistence/database.py::SqliteDatabase._migrate` and
`scripts/postgres/schema.sql`. **The Go Worker performs no migration of its
own** — it opens the store the Core already wrote and works against the
schema as-is.

## Ownership matrix

| Table | Insert (write) | Delivery outcome (write) | Read | Notes |
|---|---|---|---|---|
| `delivery_jobs` | Python pipeline (enqueue) | Go Worker **or** Python loop, never both | both, plus reconciler | consumption is flag-gated by `ATF_WORKER_ENABLED` |
| `delivery_attempts` | the engine dispatching the send | the same engine | both | shared fence; the id format is byte-identical across engines |
| `message_map` | Python pipeline at enqueue | the engine that confirmed the delivery | both, plus sync engine | only the **primary forward** may update a row — a keyword reply shares the key and must not overwrite it |
| `dead_letter_queue` | the engine that exhausted a job | replay is owner-scoped | API + bot | unchanged schema and semantics |
| `forward_rules`, `filter_rules` | Go API (through the Core's gRPC) | never | Go Worker read-only | the Worker never writes configuration |
| `sessions`, `forward_rules` metadata, everything else | Python Core | Python Core | — | untouched by this stage |

## The contract that makes dual ownership safe

The API and the Worker never independently mutate the same business table
without a documented transaction contract. Here it is:

* **The API owns configuration** (`forward_rules`, `filter_rules`, users,
  sessions). The Worker reads them; it never writes them.
* **The pipeline owns enqueue.** Only `core/application/pipeline.py` (via
  `DurableQueueManager.enqueue_delivery`) inserts `delivery_jobs` rows, in
  both modes.
* **The executing engine owns the delivery state transition.** Which engine
  that is is decided by `ATF_WORKER_ENABLED`, and there is exactly one at a
  time. The transition itself (`CLAIMED → SENT|FAILED|UNKNOWN|RETRY_WAIT`)
  and the fence writes that guard it use identical SQL and identical key
  formats in Go and Python, so a handover mid-flight cannot double-send.
* **`message_map` is updated by whichever engine confirmed the delivery**,
  under the same primary-forward-only rule.

## SQLite compatibility

The Go Worker uses the same single-connection, `_busy_timeout`,
WAL-mode access the Python Core uses (`worker/internal/queue/sqlite.go`),
so the two processes cooperate on the shared file instead of competing for
it. **PostgreSQL support is not implemented in the Go Worker at this
stage**: `openStore` returns an explicit error when `ATF_DATABASE_URL` is
set, and the Python loop remains the execution path on Postgres
deployments. SQLite-only here is a stated limitation, not an
untested claim — see `migration-plan.md` stage 2.
