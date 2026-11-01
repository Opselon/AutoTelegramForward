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

## Stage 3 — (THIS PR, stacked on PR #286)

| Capability | Status | Evidence |
|---|---|---|
| BOT Delivery Transport | **Go, done** | Narrow typed Bot API operations in `telegram_gateway.proto` (`BotSendText`, `BotCopyMessage`, `BotSendMedia`, `BotDeleteMessages`, `BotAvailability`); `telegram_runtime/adapters/bot_api` reuses `BotApiTransport` connection pool; owner-validated on every call (including cache hits); unsupported Bot API operations fall back to USER transport with explicit logging; `TestBOTDeliveryDispatchesThroughBot`, `TestBOTDeliveryFallsBackToUSERWhenUnsupported` |
| Bot Token Security | **done** | The Go Worker **never** receives, logs, or stores raw bot tokens. The wire carries `bot_token_id` + `owner_user_id`; resolution, tenant authorization (`get_owned` with owner in WHERE clause), and decryption happen strictly inside the Python Runtime (`telegram_runtime/app/bot_store.py`) |
| Branding Hop Stage 1 in Go Worker | **Go, done** | Decision logic ported to `worker/internal/domain/branding` with 24 shared parity fixtures against the live Python implementation (`TestBrandingDecisionMatchesPython`); durable enqueue happens **before** the branding side effect; intermediate post into channel C executed by Go Worker (`ensureBrandingStage1`) and checkpointed on `delivery_jobs` (`intermediate_chat_id`, `intermediate_message_id`, `delivery_stage`); resumes without duplicate post into C on restart (`TestBrandingStage1ReusesConfirmedCheckpoint`); fan-out content deduplication via normalized content digest |
| Durable Auto-Delete Scheduler | **Go, done** | Dedicated scheduled-action engine in `worker/internal/engine/scheduler`; replaces Python in-memory `asyncio.sleep`; durable `scheduled_actions` table on SQLite (migration 33) and PostgreSQL (`initialize_postgres_schema` + `schema.sql`); atomic `ClaimDue` with `FOR UPDATE SKIP LOCKED`; bounded loop and worker pool (no per-action goroutine or timer); survives Worker/Runtime restarts; USER and BOT deletion; idempotent cleanup; 14 SQLite + 4 PostgreSQL integration tests |
| Keyword Reply Delivery Compatibility | **Go, done** | Text-only dispatch compatible with BOT and USER transports; independent `job_kind` idempotency key; never overwrites primary message mapping; never copies source media; `TestKeywordReplyBOTCompatibility` |
| Forward Every N Preservation | **preserved** | Preserved at the event-ingestion level in Python (Core `forward_interval.py`); atomic counter advancement before fan-out; **not** moved to per-target Worker jobs (deferred to Stage 4 event-ingress boundary) |

## Stage 4 — Event Ingestion and Business Pipeline Extraction (Future Work)

- Move normalized Telegram update processing into Go.
- Move event-level `forward_every_n` cadence ownership.
- Move Keyword Reply matching and generation.
- Move remaining business filters and content policies.
- Establish an explicit event-level idempotency contract.
- Keep Python as the MTProto / Bot API Telegram connectivity runtime.

## Stage 5 — Final Ownership Transfer (Future Work)

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
