# Job Lifecycle, Idempotency and Uncertain Delivery

> Implemented: the Go Worker's claim/fence/retry state machine
> (`worker/internal/queue`, `worker/internal/engine/delivery`), verified by
> `worker/internal/queue/sqlite_test.go`,
> `worker/internal/queue/postgres_test.go` (26 tests against a real
> PostgreSQL) and `worker/internal/engine/delivery/delivery_test.go`.

## The state model (the existing one — no new states were invented)

```
PENDING
   |
   v
CLAIMED ──────────────> RETRY_WAIT ──> (claimable again; attempts NOT spent)
   |                        ^
   +----> SENT              |
   |                        |
   +----> FAILED            |
   |                        |
   +----> UNKNOWN ──────────┘  (reopened only by explicit reconciliation)
   |
   +----> DEAD_LETTER (attempts exhausted, replayable)
```

The important asymmetry, preserved exactly from the Python engine:

* **`Defer` never spends an attempt.** A paused task, a throttled target, an
  offline session, an unusable bot token are states of the *environment*, not
  failures of the job. Spending an attempt on them is what turned a
  ten-second session outage into a dead-lettered message reported as
  `DELIVERY_EXHAUSTED`.
* **`MarkRetry` spends one.** Only a genuine Telegram failure consumes the
  budget, and exhaustion routes the job to `dead_letter_queue` for replay.
* **`MarkUnknown` is terminal-for-now.** It is never retried automatically.

## Idempotency: three layers, one job

Duplicate execution can come from a repeated Telegram update, a duplicate API
request, a worker restart, a lease expiring mid-dispatch, a lost database
connection, a network timeout, or a repeated scheduler run. The defense is
layered:

1. **The claim** — `ClaimBatch` marks rows CLAIMED inside one atomic statement
   (`UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)` on
   PostgreSQL), so two workers cannot claim the same row and the claim never
   opens a read-then-write window. A claim error fails closed: the Python
   implementation's broad-except fallback (no row locking, no lane
   isolation, `created_at`-only ordering) is deliberately not ported.
2. **The target lane** — a job is only claimable when *no earlier unsent job
   exists for the same `target_chat_id`. One target is one lane with one
   in-flight job; different targets proceed in parallel. Ordering is
   `(source_chat_id, source_message_id, created_at, id)` on both backends —
   never `created_at` alone. (Tests: `TestTwoWorkersOneLane`,
   `TestPostgresSourceMessageOrdering`, `TestPostgresMultipleJobsOneTargetAreSerialized`.)
3. **The send-side fence** (`delivery_attempts`) — Telegram has no
   idempotency key for ordinary sends, so the logical send is claimed
   atomically *after preflight and before any Telegram call*. The key is
   deterministic and business-derived — `rule:source_chat:source_message:target[:kind]`,
   byte-identical to the Python `DeliveryAttemptKey.id` — so every claim,
   claim-after-crash and duplicate check resolves to the same row.

The fence outcomes, shared verbatim with Python:

| Outcome | Meaning | Action |
|---|---|---|
| `dispatch` | absent row, or last outcome FAILED (a replay) | send |
| `already_sent` | a previous attempt recorded SENT | **never re-send** — close the job SENT with the recorded id |
| `in_flight` | another worker holds a live claim | do not dispatch |
| `stale` | an expired claim held by somebody else | do not dispatch |
| `unknown` | an earlier send's outcome was lost | do not dispatch — reconcile |

SENT and UNKNOWN rows are **never** re-dispatched. Tests:
`TestFenceIsTheIdempotencyGuarantee`,
`TestDuplicateJobSubmissionIsNotDuplicateDelivery`.

## Uncertain delivery: the honest answer

> Go requests a Telegram delivery. Telegram accepts the message. The response
> is lost. Go does not know whether delivery succeeded.

This is **not** a condition the migration can wish away, and the
implementation does not pretend to:

* A **timeout** (`NETWORK_TIMEOUT`) is classified *ambiguous*. The job is
  marked `UNKNOWN`, the fence row is stamped `UNKNOWN`, and nothing is
  re-dispatched automatically (test: `TestTimeoutIsUnknownNeverRetriedBlindly`).
* An expired fence claim moves to `UNKNOWN`, never back to
  dispatch-by-default (`TestFenceExpiredClaimBecomesUnknownNotDispatch`) —
  the send may have landed and re-dispatching is exactly the duplicate the
  fence exists to prevent.
* The only sanctioned way to reopen an UNKNOWN fence is
  `FenceResolveUnknown`, which **records the reason** an operator or a
  reconciliation pass proved no earlier copy was in the target.

**This system provides at-most-once Telegram delivery for a single logical
send, and at-least-once for a logical delivery whose send never reached
Telegram. It deliberately does not claim exactly-once** — Telegram has no
idempotency key for ordinary sends, so that guarantee is not obtainable.
What is obtainable — and what is implemented — is that a *recoverable* job
never blindly duplicates.

## Restart safety

| Event | What survives | Why |
|---|---|---|
| Worker restart mid-flight | the job | stays CLAIMED; its lease expires; another worker re-claims; the fence guarantees the re-claim cannot double-send |
| Worker restart after Telegram accepted, before the commit | the send | the fence row was stamped SENT before the job-row commit |
| Python Runtime restart | sessions | session strings live in the Core's encrypted store; the runtime only holds them in memory; ids are stable |
| Worker restart | no re-authentication | the worker holds no session material at all |
| Database temporarily unavailable | the jobs | the queue is the database; the claim loop reports `degraded` and retries |

## Backpressure

The Python pipeline's enqueue path refuses when `pending >= ATF_QUEUE_MAX_SIZE`
(the existing behavior, unchanged). The Go Worker reports `pending_count`
through `/healthz` and its metrics surface so the same number is observable
from the execution plane.
