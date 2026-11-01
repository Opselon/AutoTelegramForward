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

See `docs/architecture/` for the boundary, the job lifecycle, and the
migration plan; `docs/runbooks/` for operations.

## Build & test

```bash
cd worker
go build ./...
go test ./...           # delivery, queue/fence, filter parity, config
go test -race ./...     # the fence/queue semantics are concurrent by construction
```

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
| `ATF_DB_PATH` | `/app/data/atf.db` | the same SQLite store the Core writes |
| `ATF_DATABASE_URL` | — | PostgreSQL: **not supported at this stage** (errors out) |
| `ATF_TELEGRAM_RUNTIME_ADDR` | `core:50053` | the Python Telegram Runtime |
| `ATF_QUEUE_NUM_WORKERS` | `4` | bounded goroutines (same name as the Python Core's) |
| `ATF_QUEUE_BATCH_SIZE` | `5` | claim batch size |
| `ATF_QUEUE_LEASE_SECONDS` | `30` | claim lease |
| `ATF_QUEUE_MAX_PER_MINUTE` | `0` (off) | per-target send rate limit |
| `ATF_MEDIA_DIR` | `/var/lib/atf/media` | shared media scratch (worker-owned) |
| `ATF_WORKER_LISTEN` | `0.0.0.0:8090` | health/metrics, internal-only |

## What is NOT implemented here (stage 1)

* The rule store is a minimal projection (`cmd/worker/main.go::ruleStore`);
  the delivery-shape inputs travel in the job payload, but the delivery-time
  rule re-read arrives in stage 2.
* PostgreSQL is refused (`openStore` in `cmd/worker/main.go`).
* The BOT delivery transport is not implemented in Go.

See `docs/architecture/migration-plan.md`.
