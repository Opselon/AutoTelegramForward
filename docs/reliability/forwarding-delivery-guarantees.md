# Forwarding Delivery Guarantees

**Scope:** the forwarding pipeline of Opselon/AutoTelegramForward, as changed by
the forwarding-reliability work (see `forwarding-pipeline-audit.md` for the
findings this document formalises).

This document states precisely what the system guarantees, what it does not,
and where the irreducible boundaries are. It is deliberately conservative: a
guarantee that cannot be proven is not claimed.

---

## 1. The guarantee model: durable at-least-once per destination

The forwarding pipeline is **at-least-once per destination**, with idempotency
and duplicate prevention wherever technically possible. It is **not
exactly-once**, and cannot be, because Telegram's Bot/MTProto APIs for ordinary
message sends accept no idempotency key.

For every eligible source message and every configured destination the system
maintains **independently observable delivery state**:

| state | meaning |
|---|---|
| `PENDING` | a durable delivery job exists; no worker has dispatched it yet |
| `CLAIMED` | a worker owns the job and is dispatching it |
| `RETRY_WAIT` | an attempt failed transiently; a bounded retry is scheduled |
| `SENT` | Telegram returned a **success response** *and* the result was persisted |
| `UNKNOWN` | the call timed out or the connection dropped — the outcome is ambiguous |
| `FAILED` | a permanent error, or the retry budget is exhausted |
| `DEAD_LETTER` | the delivery exhausted its retries and is replayable by the owner |

A delivery is **successful only when the Telegram operation returns a valid
success response and the result is persisted**. A job being dequeued, claimed,
or a send merely being initiated is never recorded as success.

**Independence (the core property):** a successful delivery to destination B
never causes destination C or D to be marked successful. Each destination has
its own `delivery_jobs` row (keyed `rule / source_chat / source_message /
target / job_kind`), its own `message_map` row, its own retry budget, and its
own send-side fence row. One destination failing never terminates processing
for the remaining destinations, in both the durable worker and the legacy
direct dispatch path.

---

## 2. What is guaranteed

### 2.1 Ingestion — no eligible source message is silently skipped

The poller and boot catch-up ask Telegram for a range of source ids. When
`GetChannelDifference` returns only part of that range:

* every **unreturned id inside the requested range** is recorded as durable
  recovery work (`ingestion_gaps`) — never as a loss;
* the **ingestion cursor advances only over ids the pipeline actually
  accepted** (the highest accepted id), never to the requested range's top;
* a structured WARN is emitted with the source, the requested range, the
  returned count, the missing id ranges and the recovery status;
* a bounded recovery sweep re-fetches each missing id with a narrow
  `min_id=max_id` range and exponential backoff + jitter;
* an id that the pipeline *does* see resolves its gap row immediately, before
  any filtering can hide it;
* an id that cannot be recovered after the bounded budget is marked **FAILED
  with an actionable reason** — visibly, never silently;
* the whole loop is crash-safe: gap rows live in the database, so a restart
  resumes recovery rather than losing it.

A numeric gap in channel message ids is **not** proof of a missing message
(deleted and service posts leave legitimate gaps), so a gap row is *recovery
work to attempt*, never a loss record.

### 2.2 Fan-out — per-destination outcomes are independent

* every destination of a rule receives its own job, its own mapping row and
  its own fence row;
* the event-level replay marker (`processed_messages`) is written **only when
  the whole fan-out was accepted** — a target whose enqueue was refused
  (backpressure) keeps the event replayable, so the repair is not blocked by
  the event's own dedup;
* the replay that repairs a missing target is safe: the job insert is
  `INSERT OR IGNORE`, so already-queued targets are no-ops and only the
  missing one is created;
* a replay of an already-delivered event **never regresses** a `SENT` mapping
  back to `PENDING`.

### 2.3 Delivery — bounded retries, classification, dead-letter

* transient failures retry with exponential backoff + jitter, capped;
* FloodWait honours Telegram's own duration and propagates the account-level
  suspension;
* timeouts and connection drops are classified **UNKNOWN**, never blindly
  retried — a send whose outcome is unknown is not re-dispatched;
* permanent failures (permissions, deleted chats, revoked accounts) fail fast
  with the owning task auto-paused and the owner notified;
* a job that exhausts its retry budget lands in the **dead-letter queue with
  its payload**, is owner-scoped, and is replayable;
* environment blockers (paused task, rate limit, time limit, no live client,
  unusable bot token) **defer** without consuming a retry attempt.

### 2.4 Crash recovery — no delivery is permanently stranded

* the job lease is the claim; a worker that dies or is restarted leaves it
  `CLAIMED` and the watchdog reclaims it after the lease expires;
* startup recovery runs once, single-replica when a coordinator is present;
* the send-side fence (below) means a reclaimed job that had already produced
  a confirmed Telegram response is closed as `SENT` rather than re-dispatched.

---

## 3. The send-side idempotency fence

Telegram offers no idempotency key for ordinary sends, so the application owns
one: a durable, database-backed per-logical-delivery claim, shared by **both**
send paths (the durable worker and the legacy direct dispatch), keyed on a
**deterministic business key** — `rule_id / source_chat_id / source_message_id
/ target_chat_id / job_kind` — never a random per-attempt id.

The state machine is `CLAIMED → SENT | FAILED | UNKNOWN`, and the claim is
atomic under concurrent workers (SQLite `INSERT ... ON CONFLICT` inside the
connection lock; PostgreSQL `ON CONFLICT DO UPDATE` with `SKIP LOCKED` at the
claim site it shares with the queue).

| fence state | what a later caller must do |
|---|---|
| **SENT** | never re-dispatch; the worker marks the job `SENT` with the recorded target id |
| **CLAIMED** (live lease, another holder) | must not dispatch concurrently |
| **CLAIMED** (expired lease, another holder) | `stale` — the watchdog moves it to UNKNOWN; no dispatch in the meantime |
| **UNKNOWN** | do **not** blindly resend; reconcile (a Telegram-side read or a bounded recovery decision), record the reason, then reopen |
| **FAILED** | retryable — a replay (e.g. a dead-letter replay) re-claims it and its attempt counter advances |

Every send path claims the key **before** any Telegram call; no path may
bypass it, and a fence store that is unreachable refuses the send loudly
rather than dispatching unprotected.

---

## 4. The irreducible ambiguity (what is NOT guaranteed)

**If Telegram accepts a send and the process dies before the success is
persisted, that delivery's outcome is ambiguous and no system can resolve it
without a Telegram-side key.** The application's answer to that boundary:

1. The confirmed result is written to the fence *immediately* (before the
   deferred batch commit), shrinking the window as far as the architecture
   allows.
2. If the process dies inside it, the fence claim expires to **UNKNOWN** —
   deliberately *not* re-dispatched automatically. A blind resend here is
   exactly how a duplicate is manufactured.
3. UNKNOWN is resolved only by an **audited** reconciliation (`resolve_unknown`
   records the reason), or by the delivery being superseded.

**Consequences that follow from the same boundary:**

* **At-least-once, not exactly-once.** A duplicate is possible in the
  UNKNOWN-window scenario even with the fence; what the fence eliminates is
  every *deterministic* duplicate: lease-expiry mid-dispatch, concurrent
  workers, legacy/worker racing, and crash-before-commit.
* Ordering is source-order *per destination* (the lane invariant). Across
  destinations, copies are delivered concurrently and can interleave.
* A message whose source post Telegram has genuinely deleted cannot be
  forwarded; its gap row records that fact as FAILED after the bounded budget.
* Delivery to a destination the owner has not granted write permission to
  fails permanently; it is never silently dropped, and the task is auto-paused
  with a reason.

---

## 5. Duplicate prevention vs. duplicate detection

| mechanism | scope | prevents |
|---|---|---|
| event-level `processed_messages` | per (rule, source) event | replaying a fully-processed event *at all* |
| `delivery_jobs` UNIQUE key | per (rule, source, target, kind) | a second job for one logical delivery |
| `INSERT OR IGNORE` enqueue | same | a replay creating a duplicate job |
| mapping `ON CONFLICT` guard | per (rule, source, target) | a replay regressing a terminal mapping |
| send-side fence (`delivery_attempts`) | per logical send | duplicate *dispatch* — the last line of defence |
| content dedup (`content_dedup`) | per (rule, content hash) | forwarding byte-identical re-posts |

The fence is the only mechanism that operates at the moment of dispatch, so it
is the only one that can stop a duplicate when the source of the duplication is
a *race or a crash*, not a duplicate enqueue.

---

## 6. Storage cost

The two new tables are **state-only** — neither stores message payloads (the
queue's `payload_json` remains the single copy).

* `delivery_attempts`: one row per logical delivery, ~150 bytes. A row is
  created on first dispatch and is **not** needed for an event that is never
  dispatched. Retention follows the queue's own retention sweep.
* `ingestion_gaps`: one row per unrecovered source id, ~120 bytes. Rows are
  **deleted** the moment the pipeline sees the id; only the small residual of
  genuinely-absent ids (deleted/service posts) accumulates, bounded by the
  retry budget.

Existing tables are untouched: no column changes, no rebuilds, no data
migration. The upgrade is additive and reversible (dropping the two new tables
returns the schema to its previous shape with no effect on any existing row).

---

## 7. Verification

Each guarantee above is pinned by a regression test that reproduces the
original defect on the unpatched code path and passes on the fixed one — see
`core/tests/test_ingestion_gap_recovery.py`,
`core/tests/test_delivery_idempotency_fence.py`,
`core/tests/test_fanout_independence.py`,
`core/tests/test_forwarding_reliability_audit.py` and
`core/tests/test_v31_migration_safety.py` (the last runs the migration against
a copy of the live production database).
