# Multi-Source and Multi-Target Routing Guarantees

**Scope:** the routing topologies the platform supports, and the isolation
guarantees between tasks that share a source.
**Evidence:** every claim below is pinned by a ground probe
(`/root/.hermes/cache/scratch/atfaudit/test_routing_ground.py` and
`test_task_isolation_ground.py`) executed against the real pipeline, real
repositories and real worker — not inferred from configuration screens.

## Supported topologies

| topology | supported | evidence |
|---|---|---|
| 1 source → 1 target | yes | baseline; every test in the suite |
| 1 source → N targets (fan-out) | yes | probe: 1→3 fan-out, all three targets received the message, all three jobs `SENT` |
| N sources → 1 target (many-to-one) | yes | probe: two rules, different sources, same target — one job per (source, target), both delivered |
| N sources → N targets | yes (composition) | fan-out and many-to-one compose: each (source message, target) pair is its own job |
| N:N via multiple tasks | yes | the per-owner routing policy (below) |
| intermediate / branding hop (A→C→B) | yes | `decide_route` BRANDING_VIA_C; the hop runs once into C, C is not a stage-2 target |
| per-target policy overrides | yes | `policy_chain(target_chat_id=…)` — a target that rejects the message is skipped, not failed |

## Task isolation: the per-owner single-match policy

One message from a source is consumed by at most **one task per tenant** — the
deterministically highest-priority one — but a task never consumes a message
on behalf of another tenant. Two tenants watching the same public channel both
forward it.

The sort key is (`priority`, `execution_order`, `created_at`), and consumption
is tracked per `owner_user_id`. Before this policy existed, whichever rule
sorted first won and the other tenant's task was starved **silently** — and
since the sort key is set by the tenant, one tenant could starve another's
tasks at will.

`multi_route=True` lets a tenant's own second task also consume the message;
it does not waive the per-owner boundary for other tenants.

**Probe-verified behaviours**

| behaviour | result |
|---|---|
| two tenants, same public channel | both tenants' tasks matched |
| same tenant, two tasks, no `multi_route` | only the highest-priority task consumed |
| same tenant, first task `multi_route=True` | both of that tenant's tasks consumed |
| task A's custom header | never appears in task B's copy (no cross-task config leak) |
| task A's blacklist | never filters task B's message |

## Fan-out guarantees

* **Independent per-destination outcomes.** One destination failing does not
  prevent the others from being processed, and one destination's success never
  marks another as delivered. Pinned by PR #274's
  `test_fanout_independence.py`.
* **Per-target anti-loop.** With many targets, one of them may be the source
  itself; that *one* target is skipped and the message still goes to every
  other target (logged `[PIPELINE:LOOP_SKIP]`), rather than dropping the
  message for all of them.
* **Per-target dedup key.** The mapping and the idempotency fence key on
  (rule, source chat, source message, target, job kind), so the same message
  delivered to two targets is two independent deliverables — not one.
* **Branding hop is target-aware.** When a hop is configured, the branding
  channel receives exactly one post and is removed from the stage-2
  destinations; a rule whose only destination is the branding channel delivers
  directly instead of posting into C and leaving it there.

## Many-to-one guarantees

The mapping's unique key is `(rule_id, source_chat_id, source_message_id,
target_chat_id)`. Because the source chat is part of the key, two different
sources forwarding into the same target are two independent mappings — no
cross-source aliasing, and an edit or delete from one source never touches the
other's copy.

## Known limitations

1. **Fan-out is one logical task's fan-out.** A message consumed by task X is
   not visible to task Y of the same tenant unless Y is reached through
   `multi_route`. This is the documented single-match policy, not a bug.
2. **Priority is per tenant.** There is no cross-tenant priority or fairness
   scheduling; each tenant's own tasks order deterministically among
   themselves.
3. **A disabled or auto-paused task consumes nothing.** The watchdog drops
   queued jobs for a deleted task (`[PIPELINE:DROP]`), so a task that goes
   away does not keep forwarding messages it accepted earlier.
4. **Routing decisions are made at ingress.** A configuration change made
   between ingress and dispatch does not retroactively change a queued job's
   delivery parameters — they are snapshotted at enqueue (E2E-6, PR #275).
   The live rule read at dispatch is a *guard* only (deleted / disabled /
   auto-paused).
