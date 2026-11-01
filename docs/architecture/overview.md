# Architecture Overview — Control Plane, Execution Plane, Telegram Runtime

> **Status:** stage 1 of the Go Worker migration is **implemented** (this
> PR). The Python Core still owns ingest and, by default, delivery
> execution. The table at the bottom of this page states exactly what moved
> and what did not.

## The system

```
USERS / CLIENTS
   |
   v
RUST WEB GATEWAY :8088 ────────────── web BFF (unchanged)
   |
   v
GO API SERVER :8080 ───────────────── control plane (unchanged): auth,
   |                                   users, rules, job creation, REST
   |  control-plane gRPC :50051 (proto/autoforward.proto, unchanged)
   v
PYTHON CORE ────────────────────────── ingest: normalization, rule matching,
   |                                   transform, ENQUEUE (still the only
   |                                   writer of delivery_jobs rows)
   v
DURABLE QUEUE (delivery_jobs) ──────── PostgreSQL | SQLite: ONE store
   |
   +──── consumed by the PYTHON DurableQueueManager (default)
   |
   +──── consumed by the GO WORKER (ATF_WORKER_ENABLED, opt-in)
            |
            |  TelegramGateway gRPC :50053 (worker/proto, tfgw/v1)
            v
        PYTHON TELEGRAM RUNTIME ─────── pyrogram / MTProto: sessions,
            |                          sends, media bytes
            v
        TELEGRAM
```

Supporting infrastructure, unchanged: PostgreSQL (durable state), Redis
(ephemeral coordination, rate-limit coordination), the Rust logger
(structured logs, its own contract), the Rust web gateway (BFF).

## The ownership principle

Every component has **one clear responsibility, one authoritative ownership
boundary, and a well-defined failure and recovery model**:

* **Go API** controls the application. It never executes a Telegram side
  effect.
* **Go Worker** owns business execution: job orchestration, routing
  decisions, filtering, retry, idempotency, recovery, backpressure,
  per-target limits. It contains no MTProto.
* **Python Telegram Runtime** owns Telegram connectivity and MTProto
  execution. It contains no business decisions and no business state.
* **PostgreSQL** owns durable application state. Redis provides temporary
  coordination only.
* **Rust** keeps its existing gateway and logging responsibilities.

## Where the migration stands

| Capability | Status | Where |
|---|---|---|
| Control plane (auth, rules, REST, job creation) | **Python Core + Go API, unchanged** | `api/`, `core/` |
| Ingest, normalization, rule matching, transform | **Python, unchanged** (legacy owner) | `core/application/pipeline.py` |
| Durable queue consumption | **both engines, one at a time** — flag-gated | `core/.../durable_queue.py` (legacy) / `worker/internal/queue` (new) |
| Delivery decision tree (forward/copy/album/modes) | **Go, ported with parity tests** | `worker/internal/engine/delivery` |
| Filter engine | **Go, parity port** | `worker/internal/engine/filter` |
| Send-side idempotency fence | **Go, shared key format** | `worker/internal/queue/sqlite.go` |
| Telegram transport (sessions, MTProto, media) | **Python Runtime, new** | `telegram_runtime/` |
| Session lifecycle | **Python, unchanged** (the Core's ClientPool) | `core/.../client_pool.py` |
| gRPC contract to Telegram | **new, `tfgw/v1`** | `worker/proto/telegram_gateway.proto` |
| AI rewrite / Replace / Clean / header-footer | **Python ingest, baked into the job payload** (legacy owner) | `core/domain/services.py` |
| BOT delivery transport | **Python (legacy)** — not yet ported | `core/.../bot_transport.py` |
| Dead-letter replay, reconciliation | **Python, unchanged** | `core/application/delivery_reconciler.py` |
| Scheduling (`forward_every_n`, delays) | **Python ingest** (legacy owner) | pipeline + AdvancedSettingsEngine |

## Non-goals, stated explicitly

This stage does not: delete any Python code, change the database schema,
change the control-plane gRPC contract, publish new ports, run both delivery
engines at once, or require any user to re-authenticate a Telegram session.
