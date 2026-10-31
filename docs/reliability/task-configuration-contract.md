# Task Configuration Contract

**Scope:** what a task (forward rule) is, how its configuration reaches a
delivery, and what invariants the system enforces on it.
**Builds on:** PR #274 (delivery pipeline), PR #275 (E2E-6), PR #276
(reconciliation).

A *task* is a `ForwardRule` — the aggregate root for one source-chat →
many target-chat routing rule. This document is the contract between the
task's configuration and the delivery it produces, written because the
expanded mission's "task isolation" requirement is really a question about
*this* boundary: can one task's configuration affect another task's delivery?

## Answer: no. Each task's delivery is computed from its own configuration.

The mechanism that makes this true is the **enqueue-time snapshot**. Every
delivery parameter a worker needs is copied into the job's payload when the
job is created:

`album_mode`, `delivery_transport`, `bot_token_id`, `inline_buttons`,
`forward_mode`, `content_mode`, `fallback_enabled`, `owner_user_id`,
`delay_seconds`, `advanced_delay_seconds`, `auto_delete_seconds`, `topic_id`,
`debug_mode`, `process_raw_text`, `button_to_text`, plus the message content
itself.

The worker is then **forbidden** from reading delivery parameters from the
live rule. What it *does* read live is a guard, and only a guard:

| live read at dispatch | purpose |
|---|---|
| `rule exists?` | a deleted task must not keep forwarding messages it accepted before deletion |
| `is_active` | a disabled task's queued jobs are failed with a reason, not delivered |
| `auto_pause reason` | an engine-paused task (dead source/target) fails its queued jobs with the reason |
| `is_paused` | a manually paused task's jobs are deferred until the pause ends |
| `owner_user_id` | tenant identity for the routing decision and the notification |

This separation is **pinned by regression** (`test_job_config_snapshot.py`,
PR #275): if a future change makes the worker read a delivery parameter from
the live rule, that test fails loudly instead of silently changing the
outcome of messages that were already accepted.

## The configuration lifecycle

```
owner edits task settings (API/bot/UI)
        ↓
forward_rules row updated (validated by the settings registry)
        ↓
messages arrive from the source
        ↓
pipeline reads the live rule at INGRESS, evaluates filters and transforms
        ↓
ENQUEUE: delivery parameters snapshotted into delivery_jobs.payload_json
        ↓
worker dispatches from the snapshot; the live rule is only the guard above
        ↓
result persisted to delivery_jobs / message_map / delivery_attempts
```

Two consequences worth stating because they answer questions owners actually
ask:

* **Editing a task does not change messages already queued.** The snapshot
  governs. An owner who switches album mode from split to album mid-flight
  gets the old behaviour for the queued message and the new behaviour for the
  next one. This is deliberate: the forwarding decision was made once, and
  undoing it under a message that was already accepted is worse.
* **Deleting a task cancels its queued work.** The guard is load-bearing: a
  deleted task's jobs are failed with `Rule no longer exists` rather than
  delivered. Before the guard existed, a deleted task kept forwarding because
  `update` was a no-op for a missing row.

## Storage and tenancy

* A task belongs to exactly one `owner_user_id`. Every read path in the API,
  the bot, the stats screen and the reconciler scopes by that owner through
  `forward_rules` — one tenant never sees another's tasks, rule names,
  delivery state or findings.
* Task deletion cascades to its delivery bookkeeping. Every table that carries
  a `rule_id` is listed in `SqliteForwardRuleRepository.DEPENDENT_TABLES` and
  in the Postgres `delete()` CTE, and a drift-guard test enforces that list
  against the schema — adding a new `rule_id` table without adding it to the
  cleanup fails the test.
* AI configurations, filters and prompt templates are themselves per-task and
  per-owner; the AI rewrite runs at ingress and its output is part of the
  snapshotted payload, so a later config edit does not re-transform a queued
  message.

## What the task panel's switches actually control

| switch | binds to | enforced |
|---|---|---|
| Message Edit: Sync / Ignore | `ignore_edits` (veto) over legacy `sync_edits` | edit propagation |
| Delete Sync | `sync_deletes` | delete propagation |
| Reply | `sync_replies` | reply threading |
| Album mode (album / first / split) | `album_mode`, snapshotted at enqueue | per-item or grouped delivery |
| Fallback (re-upload bypass) | `fallback_enabled`, snapshotted at enqueue | restricted-source recovery |
| Per-target media allow-list | `allowed_media_types`, resolved via the single media resolver | content gate (E2E-1/2/7) |

## Known limitations

1. **Per-target policy overrides exist but are not per-task isolation.** A
   target-scoped override applies to one task's own target; it cannot reach
   another task.
2. **There is no task-level rate budget.** Rate limiting is per target and per
   account (FloodWait), not per task.
3. **A paused task defers, it does not queue forever.** Jobs that outlive
   their retry budget are surfaced by reconciliation as `stuck_pending` so the
   pause does not become a silent backlog.
