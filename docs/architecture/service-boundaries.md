# Service Boundaries — Control Plane, Execution Plane, Telegram Runtime

> Status of this document: **partially implemented**. The boundary itself is
> the contract this migration establishes; the table at the end names what
> has actually moved and what is still legacy Python.

## The boundary

```
USERS / CLIENTS
        |
        v
 RUST WEB GATEWAY :8088        (BFF, unchanged)
        |
        v
 GO API SERVER :8080           (control plane, unchanged)
        |
   control-plane gRPC :50051   (proto/autoforward.proto)
        |
        v
 PYTHON CORE                   (ingest, normalization, enqueue — still Python)
        |
        v
 DURABLE QUEUE (delivery_jobs) — PostgreSQL | SQLite, the ONE store
        |
        v
 GO WORKER ENGINE              (execution plane — NEW, opt-in)
        |
   TelegramGateway gRPC        (worker/proto/telegram_gateway.proto, tfgw/v1)
        |
        v
 PYTHON TELEGRAM RUNTIME       (MTProto execution — NEW, inside the Core process)
        |
        v
 PYROGRAM / MTProto -> TELEGRAM
```

## Who owns what

| Capability | Owner | Evidence |
|---|---|---|
| Authentication, users, rules, jobs creation | Go API (`api/`) | control plane, unchanged |
| Ingest, normalization, rule matching, enqueue | Python Core (`core/application/pipeline.py`) | still the only writer of `delivery_jobs` rows |
| **Delivery-job consumption, retry, idempotency, recovery** | **Go Worker (`worker/`)** — when `ATF_WORKER_ENABLED` is set | `worker/internal/queue`, `worker/internal/engine/delivery` |
| Delivery-job consumption (legacy) | Python Core (`DurableQueueManager`) — default | `core/infrastructure/telegram/durable_queue.py` |
| MTProto requests, sessions, media bytes | Python Telegram Runtime (`telegram_runtime/`) | `telegram_runtime/adapters/pyrogram`, `telegram_runtime/app` |
| Durable application state | PostgreSQL (prod) / SQLite (local) | the **same tables** both engines read and write |
| Ephemeral coordination, rate-limit coordination | Redis | unchanged |
| Web BFF | Rust gateway | unchanged |
| Logging | Rust logger | unchanged |

## The one rule the whole migration rests on

**For every operation there is exactly one authoritative execution path.**

The Go Worker and the Python `DurableQueueManager` never both consume
`delivery_jobs`. `ATF_WORKER_ENABLED` (default **off**) moves consumption to
Go; unset leaves the Python loop exactly as it was. Enqueue — writing
`delivery_jobs` rows — stays Python's in both modes, so there is never a
second writer and never a second queue.

## What is duplicating what, deliberately

The Go filter engine (`worker/internal/engine/filter`) is a **behavioral
port** of Python's `core/domain/services.py::FilterEngine`, not a second
opinion. The port exists so a task's filter decides identically no matter
which engine executes the job; the tests in
`worker/internal/engine/filter/filter_test.go` are the parity guard.

The delivery decision tree in `worker/internal/engine/delivery/delivery.go`
is a port of `DurableQueueManager._dispatch_to_telegram` for the same reason.

## Data ownership matrix

| Table | Writer | Reader | Notes |
|---|---|---|---|
| `delivery_jobs` | Python pipeline (enqueue) | Go Worker **or** Python loop (never both) | consumption is flag-gated |
| `delivery_attempts` | Go Worker (fence) / Python loop | both | the idempotency fence, shared key format |
| `message_map` | Python pipeline (insert), Go Worker (delivery outcome) | both | only the primary forward may update a row |
| `dead_letter_queue` | Go Worker / Python loop | API + bot | replay is owner-scoped |
| `forward_rules`, `filter_rules` | Go API (via Core gRPC) | Go Worker (read-only) | the Worker never writes config |

No new tables were introduced by this migration stage. See
`data-ownership.md`.
