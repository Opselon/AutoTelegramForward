# Data Ownership

> The Go Worker migration adds **no tables, no columns, no indexes and no
> migrations**. It changes *who reads and writes* existing tables, and only
> under an opt-in flag.

## The existing stores

| Store | Production | Local / test | Selected by |
|---|---|---|---|
| PostgreSQL | the primary deployment model | — | `ATF_DATABASE_URL` |
| SQLite (`data/atf.db`) | supported local/single-host deployments | every test suite | `ATF_DB_PATH` |

Both are created and migrated by the Python Core's own
`core/infrastructure/persistence/database.py::SqliteDatabase._migrate`,
`scripts/postgres/schema.sql` and
`postgres_repositories.py::initialize_postgres_schema`. **The Go Worker
performs no migration of its own** — it opens the store the Core already
wrote and works against the schema as-is. It verifies schema compatibility
at startup (`queue.NewPostgres` → `SchemaCheck`) and fails closed when a
required table or column is missing, rather than appearing healthy while
reading a database that cannot answer its queries.

## Ownership matrix

| Table | Insert (write) | Delivery outcome (write) | Read | Notes |
|---|---|---|---|---|
| `delivery_jobs` | Python pipeline (enqueue) | Go Worker **or** Python loop, never both | both, plus reconciler | consumption is flag-gated by `ATF_WORKER_ENABLED` |
| `delivery_attempts` | the engine dispatching the send | the same engine | both | shared fence; the id format is byte-identical across engines |
| `message_map` | Python pipeline at enqueue | the engine that confirmed the delivery | both, plus sync engine | only the **primary forward** may update a row — a keyword reply shares the key and must not overwrite it |
| `dead_letter_queue` | the engine that exhausted a job | replay is owner-scoped | API + bot | the written columns (`last_error`, `attempts`) match the Python contract on both backends |
| `forward_rules` | Go API (through the Core's gRPC) | never | Go Worker read-only | the Worker never writes configuration |
| `filter_rules` | Go API (through the Core's gRPC) | never | Go Worker read-only | **always SQLite** under the current Core — see below |
| `sessions`, everything else | Python Core | Python Core | — | untouched by this stage |

## Backend selection — the hybrid model

The Go Worker resolves the authoritative backend **per table**, exactly the
way the Python Core composition root (`core/main.py`) does. One deployment
can legitimately spread these tables across two databases, and the worker
opens both rather than guessing one.

| Data | Authoritative backend | Selected by |
|---|---|---|
| `delivery_jobs` | PostgreSQL when configured; otherwise SQLite | `ATF_DATABASE_URL` |
| `delivery_attempts` | PostgreSQL when configured; otherwise SQLite | `ATF_DATABASE_URL` |
| `message_map` | PostgreSQL when configured; otherwise SQLite | `ATF_DATABASE_URL` |
| `dead_letter_queue` | PostgreSQL when configured; otherwise SQLite | `ATF_DATABASE_URL` |
| `forward_rules` | PostgreSQL **only when** the Core opted the config repositories in; otherwise SQLite | `ATF_DATABASE_URL` **and** `ATF_POSTGRES_ALL` |
| `filter_rules` | **SQLite, always** | the Core composition root — no PostgreSQL table exists |
| Telegram sessions | Python Core | never migrated |

Two consequences the worker honors rather than papers over:

1. With `ATF_DATABASE_URL` set and `ATF_POSTGRES_ALL` unset, the rule store
   holds **two** databases at once: PostgreSQL `forward_rules` + SQLite
   `filter_rules`. `worker/internal/rulestore` opens one handle per
   authoritative backend; it does not assume a single database.
2. `filter_rules` has no PostgreSQL counterpart, and the worker does **not**
   create one. Creating a second source of truth for configuration the Core
   deliberately keeps in SQLite would be a correctness bug, not a
   convenience.

**ATF_POSTGRES_ALL does not apply to every table.** It moves the config
repositories; it does not move `filter_rules`, and reading it as "everything
is PostgreSQL" would point the worker's filter reads at a table that does
not exist there.

## The contract that makes dual ownership safe

The API and the Worker never independently mutate the same business table
without a documented transaction contract. Here it is:

* **The API owns configuration** (`forward_rules`, `filter_rules`, users,
  sessions). The Worker reads them; it never writes them. It is not an
  alternative API for editing users' rules.
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
it. A fully-PostgreSQL deployment still keeps the SQLite volume, because
`filter_rules` lives there and the Core initializes the file in every
configuration.

## PostgreSQL compatibility

PostgreSQL is supported for the delivery store as of Stage 2, against the
schema `initialize_postgres_schema` and `scripts/postgres/schema.sql`
produce — the worker adds nothing. Concurrency semantics preserve the Python
implementation's atomic claim: one `UPDATE ... WHERE id IN (SELECT ... FOR
UPDATE SKIP LOCKED)` statement with the same NOT-EXISTS target-lane
predicate and the same `(source_chat_id, source_message_id, created_at, id)`
ordering. The Python fallback path after a broad `except` (a read-then-update
with no row locking and `created_at`-only ordering) is deliberately **not**
ported; the Go implementation fails closed on any claim error instead.
