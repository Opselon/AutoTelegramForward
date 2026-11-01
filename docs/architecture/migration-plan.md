# Migration Plan — Strangler-Fig Extraction of the Python Core

> Stage 1 established the execution boundary. Stage 2 makes that boundary
> trustworthy for the real production database and real user configuration.
> This is the map for every remaining engine, so a reviewer can see the whole
> path, not just the current step.

## The invariant that never breaks

For every operation there is **exactly one authoritative execution path**.
No capability is ever owned by Go and Python at the same time; nothing is
ever deleted from Python until the Go replacement has demonstrated
behavioral parity; rolling back is always a config flip, not a redeploy.

## Stage 1 — (DONE)

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

### What was NOT done in stage 1 (and was not claimed)

* **The rule store was a stub.** `cmd/worker/main.go::ruleStore` returned a
  minimal always-active projection. The delivery-shape inputs
  (`forward_mode`, `content_mode`, `fallback_enabled`, `topic_id`, ...) all
  travel in the job payload — which is exactly what the Python dispatch path
  reads — so the delivery path was correct, but the *delivery-time re-check*
  of a task's active/paused state happened through that projection, not
  through a real read of `forward_rules`. **Stage 2 wires the real read.**
* **PostgreSQL was refused** (`openStore` returned an explicit error when
  `ATF_DATABASE_URL` was set): the store was SQLite-only until the
  PostgreSQL claim/fence SQL was ported and parity-tested. The Python loop
  remained the execution path on Postgres deployments.
* **The BOT delivery transport was not implemented in Go.** A job whose
  payload says `delivery_transport: BOT` is not yet dispatched through the
  bot route; leave the Python loop on for tasks that use it.
* **AI rewriting, Replace rules, Clean rules, header/footer formatting and
  keyword replies run on the Python ingest side and are baked into the job
  payload.** The Go worker applies the *result*, which is correct; it does
  not (yet) own the computation.

## Stage 2 — rule store + PostgreSQL (DONE)

| Capability | Status | Evidence |
|---|---|---|
| PostgreSQL delivery store (full `Store` interface) | **done** | `worker/internal/queue/postgres.go` |
| Claim concurrency + ordering over `FOR UPDATE SKIP LOCKED` | **done** | 26-test integration suite against a real postgres |
| Idempotency fence on PostgreSQL (byte-identical key) | **done** | `FenceClaim` + fence tests, both backends |
| Backend selection matches the Python composition root | **done** | `cmd/worker/main.go::openStore`/`openRuleStore`, `data-ownership.md` |
| Real rule store (no stub) | **done** | `worker/internal/rulestore` |
| ForwardRule persisted state read | **done** | active/paused/paused-until/auto-pause from `metadata` JSON |
| FilterRule persisted state read | **done** | legacy pre-v30 rows stay enabled (`is_enabled` compatibility) |
| Fence ordering (preflight before claim) | **done** | `fence_ordering_test.go` regression suite |
| Offline cross-engine parity harness | **done** | `worker/internal/parity` + 17 shared fixtures |
| Real PostgreSQL integration in CI | **done** | `go-worker-postgres-integration` job (postgres:17 service) |
| Docker/compose backend wiring | **done** | profile-gated postgres service, `required:false` dependency |

### Stage 2 scope boundaries (deliberately not done)

* **The Python Core remains the schema/bootstrap owner.** The worker verifies
  schema compatibility at startup and fails closed when a required object is
  missing; it never runs a migration of its own.
* **The unsafe Python claim fallback is not ported.**
  `PostgresDeliveryQueueRepository.claim_batch` carries a broad-except
  fallback that degrades to a read-then-update with no `SKIP LOCKED`, no lane
  isolation and `created_at`-only ordering. The Go implementation fails
  closed on any claim error instead — reproducing that fallback would
  convert a real production SQL error into an unprotected claim.
* **No BOT transport, no Stage 3/4 work** (below).
* **The worker stays opt-in.** `ATF_WORKER_ENABLED` still defaults to off;
  the Python `DurableQueueManager` loop is untouched and remains the
  fallback.

## Stage 3 — BOT transport, branding-hop stage-1, advanced settings (future)

* `delivery_transport: BOT` through the runtime contract (a `SendMedia`-style
  RPC that posts through the attached bot token).
* The branding hop's *stage 1* (the post into channel C) still runs on the
  Python dispatch path; the worker already reproduces stage 2
  (`brandingStage`).
* `forward_every_n`, keyword replies, auto-delete scheduling: owned by the
  pipeline today, eventually a worker-side scheduler.

## Stage 4 — ownership transfer (future)

With parity demonstrated over a full release cycle, flip the default of
`ATF_WORKER_ENABLED` to `on` and delete the Python `DurableQueueManager`
loop. **That is a separate PR with its own evidence — it is not part of this
one.**

## Shadow mode

Shadow mode is **not enabled**, deliberately: the observable decision surface
is already fully covered by the offline parity harness (the filter decisions,
the fence, the queue semantics, the claim ordering — see
`worker/internal/parity`), and a shadow mode that duplicated a *Telegram side
effect* would violate the one-authoritative-path rule. The parity harness
compares **decisions and state transitions, never side effects**: it runs
both engines against the same fixtures and normalizes the outputs, with no
session, no network and no production data touched.

## Rollback

1. `ATF_WORKER_ENABLED=0` (or unset) — the Python loop resumes; jobs already
   CLAIMED by the Go worker are reclaimed when their leases expire.
2. The fence guarantees that handover cannot double-send: a job whose send
   the Go worker recorded as SENT is `already_sent` for the Python loop too,
   because the key format is byte-identical (`rule:src_chat:src_msg:target[:kind]`).
3. No database migration to undo — stage 2 adds **no tables, no columns, no
   indexes**. The schema it reads is the one the Python Core already owns.
