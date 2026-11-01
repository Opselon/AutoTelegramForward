# Queue Recovery — runbook

## The short version

The queue **is** the database: `delivery_jobs` in the same PostgreSQL or
SQLite store the Core has always used. There is no second queue to lose and
no second broker to operate. Recovery is therefore "start a consumer
again"; the rows are already safe.

## Recovering from a crashed worker

```
jobs in CLAIMED with lease_until < now
        |
        v  (the claim query's third predicate)
   re-claimable immediately
        |
   the fence answers first:
        already_sent  -> job closed SENT with the recorded target id
        unknown       -> job stays UNKNOWN for reconciliation
        dispatch      -> the send proceeds
```

Nothing requires manual action. If you want it faster, lower
`ATF_QUEUE_LEASE_SECONDS` for the replacement.

## Recovering from a crash *between* Telegram's accept and the commit

This is the case the fence exists for. The fence row was stamped SENT
*before* the job-row write, so:

* on the next claim, `FenceClaim` answers `already_sent`;
* the job is closed SENT with the recorded target message id;
* **no second message is sent.**

If the process died *before* recording the outcome, the fence row stays
CLAIMED and the lease sweep moves it to **UNKNOWN** — deliberately never
back to dispatch-by-default, because the send may have landed. Reconciliation
(`DeliveryReconciler`, unchanged) reports it; `FenceResolveUnknown` is the
only sanctioned reopen, and it records *why*.

## Recovering `UNKNOWN` deliveries

An UNKNOWN job is not a failure — it is an honest "we do not know". Never
"fix" it by re-dispatching blindly: that is exactly how duplicate messages
are made.

1. Look the delivery up in the target (the job carries the target chat and
   the source message id).
2. If the copy is there: mark the job resolved (the mapping already carries
   the target id if the send was recorded).
3. If it is not: reopen the fence with a reason and let the queue deliver.

## Split-album partial delivery

A `split` album that died on item 3 persisted items 1 and 2's confirmations
into its own payload (`album_sent`). The retry re-sends **only the missing
items** — never the ones Telegram already accepted (audit finding E2E-5).
If you inspect the payload you will see the `"album_sent": {...}` map.

## Dead letters

`dead_letter_queue` rows are created by both engines under the same schema.
Replay is owner-scoped through the API or the bot (the existing behavior,
unchanged) and restores the job's full attempt budget.

## Redis loss

* **Single-replica deployments:** unaffected — Redis was never required.
* **Multi-replica:** the Python loop's lease coordination degrades to
  single-replica behaviour. The claim itself is in the database either way:
  a lease can be lost while it is held, so it was never what made delivery
  idempotent (the fence is).
