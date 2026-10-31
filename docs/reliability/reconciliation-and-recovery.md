# Delivery Reconciliation and Health Monitoring

**Scope:** Opselon/AutoTelegramForward — the delivery pipeline's checker.
**Builds on:** `docs/reliability/forwarding-pipeline-audit.md` and
`docs/reliability/forwarding-delivery-guarantees.md` (PR #274, merged `78b29bd`)
and `docs/reliability/end-to-end-forwarding-audit.md` (PR #275).

Durability is not observability. The pipeline can durable-enqueue a job, have
a worker claim it, and then lose the worker to a crash the lease watchdog has
not yet recovered — no failure record is produced anywhere, because nothing
failed *yet*. A rule can be paused mid-fan-out, an enqueue can fail after the
event was marked processed, and an ingestion gap can exhaust its recovery
budget while still retrying. These are the silent classes the reported
incident belongs to, and this module is what eventually notices them.

## Design principle: reconcile, never repair

The reconciler is deliberately **read-only**. It does not retry jobs, does not
advance cursors, does not write mappings. Repair belongs to the components
that already own the lifecycle and its invariants:

* the **lease watchdog** recovers expired claims,
* the **gap sweep** in `ChannelPollingService` recovers unreturned ids,
* the **dead-letter queue** terminates exhausted deliveries,
* the **recovery replay** re-drives anything `mark_processed` released.

A second repair path would have to duplicate every one of those invariants to
be safe, and would inevitably drift from them. Instead the reconciler answers
the mission's actual requirement — *if an eligible source message has not
reached its expected destination, the system must eventually produce an
actionable failure record* — by making the gap **visible**, in bounded
aggregates over tables the pipeline already owns.

No new table, event history, or payload store was introduced.

## The six checks

| check | flags | severity basis |
|---|---|---|
| `orphan_mappings` | a `message_map` row whose `delivery_jobs` counterpart vanished | the lifecycle writes both together, so one without the other is a state mismatch nothing else resolves |
| `stuck_pending` | a PENDING/RETRY_WAIT job whose `next_retry_at` is long past the retry budget | pending forever with no terminal outcome and no recorded failure is the silent class |
| `expired_claims` | a CLAIMED job whose lease is more than two lease periods past renewal | the watchdog will recover it, but until it does the delivery has no observable outcome |
| `unknown_deliveries` | a job left in `UNKNOWN` | a timed-out send resolved to neither success nor failure, and UNKNOWN has no recovery path of its own |
| `repeated_failures` | a destination or source whose failure rate is ≥ 50% | a global failure count cannot distinguish one bad target from a broken rule |
| `open_gaps` | an `ingestion_gaps` row that used its whole attempt budget without closing | a gap is recovery work, not a loss record; exhausting the budget is the actionable form |

Every finding carries the identifiers the mission's logging section requires:
`kind`, `rule_id`, `source_chat_id`, `source_message_id`,
`destination_chat_id`, `delivery_job_id`, `worker_id`, `attempt_number`,
`error_category`, `error_detail`, `retryable`, and `timestamp`. Findings never
carry message text (asserted by
`test_a_finding_reaches_the_client_with_identifiers`).

## Aggregated metrics

`health_metrics()` renders exactly the mission's list, computed as bounded
aggregates — never by materialising rows:

`eligible_processed`, `pending`, `delivered`, `failed`, `retrying`,
`dead_letter`, `success_rate`, `oldest_pending_age_seconds`,
`failure_rate_by_destination`, `failure_rate_by_source`.

Two storage-efficiency points worth stating explicitly, because the mission
asked:

1. **No raw message history is kept.** Every metric is a `GROUP BY` aggregate
   over `delivery_jobs`, which the pipeline already persists. No payloads are
   copied and no per-event rows are appended.
2. **The report is computed on demand.** Reconciliation is not a scheduled
   writer, so it contributes **zero** steady-state write amplification. The
   only cost is the read, bounded by the `GROUP BY` and the indexes the queue
   already has.

## Where the results go

The reconciler is wired into the existing `DeliveryControlServicer`, and the
gRPC contract was extended **additively** (`DeliveryStatsResponse` fields
11–19, plus the new `DeliveryFinding` message). Per the mission's rule against
breaking gRPC changes:

* older clients ignore unknown fields, so nothing breaks;
* a pre-v32 Core simply leaves the fields zero-valued — the Go handler and the
  dashboard render them without a version gate;
* a reconciler that itself fails degrades the additive fields to zero rather
  than failing `GetDeliveryStats` (pinned by
  `test_a_reconciler_failure_does_not_break_the_stats`);
* the reconciler is an **optional** collaborator: a servicer constructed
  without one still answers (pinned by
  `test_a_servicer_without_a_reconciler_still_answers`).

The Go API forwards the fields to `/api/queue` unchanged; the response shape
grows but never renumbers.

## Tenancy

Reconciliation is owner-scoped by the same rule ownership the servicer already
enforces: `_scope()` resolves the owner's rule ids from `forward_rules` and
bounds every query with them. An owner with no rules gets a finite, empty
report instead of a full-database read. Pinned by
`test_an_owner_only_sees_their_own_findings`.

This matters beyond politeness: the reconciler reads `delivery_jobs`, and
without scoping it would expose one tenant's delivery state through another
tenant's stats screen — the exact class PR #274's tenant work closed.

## Failure isolation

Each check runs in its own `try/except`; a check that raises contributes an
empty list and is itself *reported* (`[PIPELINE:RECONCILE_CHECK_FAILED]`),
never silently omitted. A reconciliation that quietly drops a check would be
worse than one that says it broke. Pinned by
`test_one_failing_check_does_not_blank_the_report`.

## What this is not

* **Not a trigger.** Nothing here fires on a message event; it is a periodic
  or on-demand *read*. The scheduled cadence, when one is added, belongs in
  the startup wiring so the poller and the watchdog keep their existing
  precedence.
* **Not exactly-once.** A finding describes a state, not an event; the same
  stranded job will be flagged on consecutive runs until the watchdog recovers
  it. That is by design — the finding is a *reminder*, and it stops the moment
  the state is gone.
* **Not a substitute for the audit trail.** The `error_log` and the
  dead-letter queue remain the record of *why* a delivery failed; the
  reconciler reports *that it has no outcome*.

## Regression tests

| file | covers |
|---|---|
| `core/tests/test_delivery_reconciliation.py` (16) | every check's positive *and* negative contract, tenancy, failure isolation, aggregate correctness |
| `core/tests/test_grpc_reconciliation_surface.py` (6) | the additive gRPC fields, owner scoping end-to-end, graceful degradation, wiring parity with `main.py` |

All 22 pass against the real SQLite repositories and the real servicer; no
mocks.
