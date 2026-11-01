# Migration Plan — Strangler-Fig Extraction of the Python Core

> This is the plan the current PR implements **stage 1** of. It is written as
> the map for every remaining engine so a reviewer can see the whole path,
> not just the first step.

## The invariant that never breaks

For every operation there is **exactly one authoritative execution path**.
No capability is ever owned by Go and Python at the same time; nothing is
ever deleted from Python until the Go replacement has demonstrated
behavioral parity; rolling back is always a config flip, not a redeploy.

## Stage 1 — this PR (DONE)

| Capability | Status | Evidence |
|---|---|---|
| Durable queue consumption (claim/lane/order) | **Go, done** | `worker/internal/queue/sqlite.go` + 12 queue tests |
| Send-side idempotency fence | **Go, done** | `FenceClaim/RecordSent/ExpireStale/ResolveUnknown` + tests |
| Delivery decision tree (forward/copy/album/content modes) | **Go, done** | `worker/internal/engine/delivery/delivery.go` + 15 delivery tests |
| Restricted-source fallback (copy → re-upload) | **Go, done** | `reuploadMedia` + `TestMediaForwardCopyThenFallback` |
| Retry policy (FloodWait defer, backoff, budget, dead-letter) | **Go, done** | `handleFailure` + `TestFloodWaitDefersWithoutSpendingAttempt` |
| Uncertain-delivery handling (UNKNOWN, never blind retry) | **Go, done** | `TestTimeoutIsUnknownNeverRetriedBlindly` |
| Versioned Telegram Gateway contract | **done** | `worker/proto/telegram_gateway.proto` (`tfgw/v1`) |
| Python Telegram Runtime (pyrogram adapter, error taxonomy) | **done** | `telegram_runtime/` + 16 runtime tests |
| Strangler switch (`ATF_WORKER_ENABLED`, default off) | **done** | `DurableQueueManager.start()` + `test_worker_switch.py` |
| Filter engine port (parity-tested) | **Go, done** | `worker/internal/engine/filter` + 15 filter tests |
| Docker / compose / CI integration | **done** | `Dockerfile.worker`, compose service, 2 new CI jobs |
| Activation | **off by default** | flip `ATF_WORKER_ENABLED` |

### What is NOT done in stage 1 (and is not claimed)

* **The rule store is a stub.** `cmd/worker/main.go::ruleStore` returns a
  minimal always-active projection. The delivery-shape inputs
  (`forward_mode`, `content_mode`, `fallback_enabled`, `topic_id`, ...) all
  travel in the job payload — which is exactly what the Python dispatch path
  reads — so the delivery path is correct, but the *delivery-time re-check*
  of a task's active/paused state happens through that projection, not
  through a real read of `forward_rules`. **Stage 2 wires the real read.**
* **PostgreSQL is refused for now** (`openStore` returns an explicit error
  when `ATF_DATABASE_URL` is set): the store is SQLite-only until the
  PostgreSQL claim/fence SQL is ported and parity-tested. The Python loop
  remains the execution path on Postgres deployments.
* **The BOT delivery transport is not implemented in Go.** A job whose
  payload says `delivery_transport: BOT` is not yet dispatched through the
  bot route; leave the Python loop on for tasks that use it.
* **AI rewriting, Replace rules, Clean rules, header/footer formatting and
  keyword replies run on the Python ingest side and are baked into the job
  payload.** The Go worker applies the *result*, which is correct; it does
  not (yet) own the computation.

## Stage 2 — rule store + PostgreSQL (next PR)

1. Replace `ruleStore` with a read over `forward_rules` / `filter_rules`
   (SQLite first, then PostgreSQL), reusing the Go API's existing
   repository patterns.
2. Port the claim/fence SQL to PostgreSQL, mirroring
   `postgres_repositories.py` byte-for-byte (same lock semantics —
   `SELECT ... FOR UPDATE SKIP LOCKED` where Python uses it, or the same
   NOT-EXISTS lane predicate under READ COMMITTED).
3. Add a `worker`-side parity harness: run both engines against the same
   fixture database and compare decisions (shadow mode, §Shadow below).

## Stage 3 — BOT transport, branding-hop stage-1, advanced settings

* `delivery_transport: BOT` through the runtime contract (a `SendMedia`-style
  RPC that posts through the attached bot token).
* The branding hop's *stage 1* (the post into channel C) still runs on the
  Python dispatch path; the worker already reproduces stage 2
  (`brandingStage`).
* `forward_every_n`, keyword replies, auto-delete scheduling: owned by the
  pipeline today, eventually a worker-side scheduler.

## Stage 4 — ownership transfer

With parity demonstrated over a full release cycle, flip the default of
`ATF_WORKER_ENABLED` to `on` and delete the Python `DurableQueueManager`
loop. **That is a separate PR with its own evidence — it is not part of this
one.**

## Shadow mode

Shadow mode is **not enabled** in this stage, deliberately: the observable
decision surface is already fully covered by the parity test suites (the
filter port, the fence, the queue semantics), and a shadow mode that
duplicated a *Telegram side effect* would violate the one-authoritative-path
rule. Shadow mode becomes useful in stage 2+, when the *decision* can be
compared offline (normalize both decisions and diff) with no side effect at
all — the comparison target is the job rows, not the target chats.

## Rollback

1. `ATF_WORKER_ENABLED=0` (or unset) — the Python loop resumes; jobs already
   CLAIMED by the Go worker are reclaimed when their leases expire.
2. The fence guarantees that handover cannot double-send: a job whose send
   the Go worker recorded as SENT is `already_sent` for the Python loop too,
   because the key format is byte-identical.
3. No database migration to undo — stage 1 adds **no tables, no columns, no
   indexes**.
