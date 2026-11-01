# ATF Go Worker

The business **execution plane** of AutoTelegramForward: it consumes the
durable delivery queue (`delivery_jobs`) and executes deliveries through the
Python Telegram Runtime over a versioned gRPC contract.

* **Independent module** — `worker/go.mod`, builds and tests on its own.
* **Independent container** — `Dockerfile.worker`, non-root, no Python, no
  Telegram library inside.
* **Opt-in** — `ATF_WORKER_ENABLED` defaults to **off**; the Python Core
  keeps its delivery loop and this worker consumes nothing. That is the
  strangler-fig guarantee: one authoritative execution path at a time, and
  rollback is a config flip.

See `docs/architecture/` for the boundary, the job lifecycle, the data
ownership model and the migration plan; `docs/runbooks/` for operations.

## Build & test

```bash
cd worker
go build ./...
go test ./...           # delivery, queue/fence, filter parity, rule store, config
go test -race ./...     # the fence/queue semantics are concurrent by construction
```

### PostgreSQL integration tests

The adapter's correctness cannot be proven by SQLite: `FOR UPDATE SKIP
LOCKED`, JSONB payload decoding, `DOUBLE PRECISION` lease arithmetic and the
`delivery_attempts` UPSERT are all engine-specific. The integration suite
runs against a **real** PostgreSQL, gated on a DSN:

```bash
docker run --rm -d --name atf-pg -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=postgres -p 55432:5432 postgres:17-alpine

cd worker
ATF_TEST_POSTGRES_DSN=postgres://postgres:test@127.0.0.1:55432/postgres \
  go test ./internal/queue/ -run '^TestPostgres' -v
```

Without the variable the suite **skips** (a local `go test ./...` stays
green without a database); CI arms it against a real postgres service.

Each test creates its own scratch database, applies the production queue
schema, and drops it on cleanup — no shared state, no half-mutated schema
left behind by a failed run.

### Cross-engine parity

`worker/internal/parity` runs 17 shared JSON fixtures through **both** the Go
filter engine and the real Python `FilterEngine` (`core.domain.services` —
the live pipeline's own class, not a shadow implementation) and compares the
normalized decisions. It is offline, deterministic, and touches no session,
no network and no production data.

```bash
# Regenerate the committed reference after a fixture or a Python behavior change
scripts/regen_parity_reference.sh
```

CI regenerates the reference and diffs it against the committed artifact, so
a Python behavior change nobody re-referenced fails the build instead of
diverging silently.

## Backend selection

The worker resolves the authoritative backend per table, exactly as the
Python Core composition root does (`core/main.py`):

| Data | Backend | Selected by |
|---|---|---|
| delivery queue (`delivery_jobs`, `delivery_attempts`, `message_map`, `dead_letter_queue`) | PostgreSQL, else SQLite | `ATF_DATABASE_URL` |
| `forward_rules` | PostgreSQL **only when the Core opted in**; else SQLite | `ATF_DATABASE_URL` **and** `ATF_POSTGRES_ALL` |
| `filter_rules` | **SQLite, always** | the Core serves it from SQLite; no PostgreSQL table exists |

The worker therefore holds **two** databases when `ATF_DATABASE_URL` is set
and `ATF_POSTGRES_ALL` is not (a PostgreSQL delivery store + a SQLite rule
store). That is deliberate, not an oversight. `ATF_POSTGRES_ALL` does not
apply to every table — reading it as "everything is PostgreSQL" would point
filter reads at a table that does not exist there.

On startup, the PostgreSQL store pings the database and verifies the
required tables and columns exist (`SchemaCheck`). A worker pointed at the
wrong database — or one the Core has not bootstrapped — refuses to start
rather than appearing healthy while doing nothing.

## Connection pool

PostgreSQL connections are bounded: 16 open / 8 idle, 30-minute lifetime,
5-minute idle lifetime. All queries are parameterized and context-aware; the
claim, the fence UPSERT and the dead-letter transition each run as one
atomic statement or transaction. No DSN is ever logged — startup errors have
their credentials stripped before they reach the log.

## Regenerating the contract stubs

Both consumers compile from the same `.proto`; the generated code is
committed and CI verifies it is not stale.

```bash
# Go
protoc --proto_path=worker/proto \
  --go_out=worker/proto/gen --go_opt=paths=source_relative \
  --go-grpc_out=worker/proto/gen --go-grpc_opt=paths=source_relative \
  worker/proto/telegram_gateway.proto

# Python
python -m grpc_tools.protoc -Iworker/proto \
  --python_out=worker/proto/gen/python \
  --grpc_python_out=worker/proto/gen/python \
  worker/proto/telegram_gateway.proto
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ATF_WORKER_ENABLED` | `0` | **the switch**. Off = the Python loop owns delivery |
| `ATF_CORE_RPC_TOKEN` | — | **required when enabled**; empty = refuse to start |
| `ATF_DB_PATH` | `/app/data/atf.db` | the config database the Core always initializes (holds `filter_rules`) |
| `ATF_DATABASE_URL` | — | PostgreSQL delivery store; also `forward_rules` **and** `ATF_POSTGRES_ALL` together |
| `ATF_POSTGRES_ALL` | `0` | moves `forward_rules` to PostgreSQL. **Never applies to `filter_rules`** |
| `ATF_TELEGRAM_RUNTIME_ADDR` | `core:50053` | the Python Telegram Runtime |
| `ATF_QUEUE_NUM_WORKERS` | `4` | bounded goroutines (same name as the Python Core's) |
| `ATF_QUEUE_BATCH_SIZE` | `5` | claim batch size |
| `ATF_QUEUE_LEASE_SECONDS` | `30` | claim lease |
| `ATF_QUEUE_MAX_PER_MINUTE` | `0` (off) | per-target send rate limit |
| `ATF_MEDIA_DIR` | `/var/lib/atf/media` | shared media scratch (worker-owned) |
| `ATF_WORKER_LISTEN` | `0.0.0.0:8090` | health/metrics, internal-only |

## Deploying it (the one non-stock step)

The worker shares the Core's `atf.db`, which the Core creates as root while
the worker runs as `atf` (uid 100 / gid 101). Before the first start:

```bash
docker compose --profile worker run --rm worker-store-init
```

Grants group write access on the store (idempotent). After that, setting
`ATF_WORKER_ENABLED=1` is the whole switch. A worker on an unwritable
store refuses to start — by design: the readiness surface must not lie.

To run the queue on PostgreSQL instead, set `ATF_DATABASE_URL` on **both**
the Core (which owns the schema bootstrap) and the worker:

```bash
# .env  (the schema is applied by the Core; the worker only verifies it)
ATF_DATABASE_URL=postgres://atf:***@postgres:5432/atf_db
ATF_POSTGRES_ALL=1   # optional: forward_rules on PostgreSQL too
ATF_WORKER_ENABLED=1
```

The SQLite volume stays: `filter_rules` lives there, and the Core initializes
`atf.db` in every configuration.

## Schema compatibility

The worker adds **no tables, no columns, no indexes**. It reads the schema
the Python Core owns and fails closed when a required object is missing. If
`SchemaCheck` reports a missing table or column, the fix is to let the Core
bootstrap the database (`initialize_postgres_schema` / `schema.sql`), never
to add a migration on the worker side.

## Rollback

1. `ATF_WORKER_ENABLED=0` — the Python loop resumes; CLAIMED jobs are
   reclaimed when their leases expire, and the fence guarantees the
   handover cannot double-send (the key format is byte-identical).
2. No database migration to undo.

## What is NOT implemented here (stage 2 → stage 3)

* The rule store, the PostgreSQL store and the parity harness **are**
  implemented; see `docs/architecture/migration-plan.md`.
* The BOT delivery transport is not implemented in Go.
* AI rewriting, replace/clean rules, `forward_every_n`, keyword-reply
  scheduling and auto-delete scheduling remain on the Python ingest side,
  baked into the job payload.
