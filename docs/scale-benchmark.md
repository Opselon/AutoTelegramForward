# Multi-tenant scale — measured, at 10,000 tenants

Readiness for 10,000 users cannot be argued from the architecture, so it is
measured. Every number below came from a run performed in the session that wrote
this file, on this host, with the harness described here. Nothing is estimated
or extrapolated; what could **not** be measured is named in
[What this does not cover](#what-this-does-not-cover).

## How to reproduce

```bash
python scripts/benchmark_scale.py --users 10000 --tasks-per-user 2 \
    --messages 5000 --concurrency 24 --iterations 40 --json scale-10k.json
```

CI runs the small profile (`--users 200 --messages 300`), which is big enough for
the query planner to choose indexes and to catch a per-tenant N+1, and small
enough to finish in seconds:

```bash
python scripts/benchmark_scale.py --users 200 --messages 300 --concurrency 8
```

The harness builds a throwaway SQLite database in a temp directory through the
**real repositories** and drives the **real** `DurableMessagePipeline` with a
stub client pool — no network, no Telegram, no live database, no synthetic
timings. It exits non-zero if a scale invariant is violated, so it is a test as
well as a measurement: cross-tenant delivery, a forwarding path that forwards
nothing, a hot-path read that became a full scan, an N+1 that grows with tenant
count, or an unbounded per-message statement count all fail the run.

## Environment

| | |
| --- | --- |
| Host | 2 vCPU container on the deployment host |
| Python | 3.11.17 |
| SQLite | 3.46.1 |
| Dataset | 10,000 tenants × 2 tasks = 20,000 rules, 10,000 bot tokens, one session |
| Messages | 5,000 synthetic text messages across the tenants' source chats |
| Concurrency | 24 in-process async workers |

## Seeding (onboarding 10,000 tenants)

| Metric | Measured |
| --- | --- |
| Tenants | 10,000 (each: 1 bot token with defaults + 2 tasks with settings) |
| Tasks written | 20,000 |
| Wall time | **1.99 s** |
| Throughput | **10,056 tasks/s** (5,028 tenants/s) |
| Process RSS after seeding | **53.0 MB** |
| Database size (incl. WAL) | **20.41 MB** |

That is 20.4 MB for 10,000 tenants — a little over 2 KB of configuration per
tenant. Nothing about the data model forces the process to hold it in memory:
seeding 10,000 tenants costs 53 MB, and the numbers below do not move when it
does.

## Hot-path reads (what one message and one screen cost)

Percentiles over 40 calls, each against the *worst* tenant (the last one added,
i.e. the deepest in every index).

| Read | p50 | p95 | p99 | SQL statements/call |
| --- | --- | --- | --- | --- |
| `list_active_by_source` — the per-message lookup | 0.093 ms | 0.137 ms | 0.151 ms | **1** |
| `list_by_owner` — a tenant's own tasks | 0.090 ms | 0.121 ms | 0.227 ms | **1** |
| `list_by_owner(limit=50)` — one page | 0.088 ms | 0.104 ms | 0.113 ms | 1 |
| `count_by_owner` — pages, without reading rows | — | — | — | 1 |
| `get_by_id` — resolving an attached bot | < 0.001 ms | < 0.001 ms | < 0.001 ms | 1 |
| effective settings (per-bot defaults + task overrides) | 0.035 ms | 0.060 ms | 0.171 ms | **1** |

The constant that matters is **statements per call, not milliseconds**: it is
what says "no N+1, no per-tenant scan". One statement per read, at 10,000
tenants, for every one of them. The ingest phase below asserts the same property
end to end at 4.0 statements per message.

## Ingest (the control plane's ceiling)

| Metric | Measured |
| --- | --- |
| Messages | 5,000 (concurrency 24) |
| Wall time | 2.64 s |
| Throughput | **1,894 messages/s** accepted and queued |
| Latency | p50 0.297 ms, p95 0.477 ms, p99 6.094 ms, max 12.139 ms |
| SQL statements per message | **4.0** |
| Forwarded | 5,000 / 5,000 |
| Delivery jobs queued | 5,000 (left for the delivery worker; not drained here) |
| Cross-tenant deliveries | **0** |
| Process RSS | 73.6 MB |

Every one of the 5,000 jobs was traced back to its rule and checked against the
source chat's owner: forwards never crossed a tenant boundary in this run, and
the check fails the benchmark rather than printing a warning.

## The bottleneck this found, and the fix

The first 10,000-tenant run exposed one defect that unit tests cannot see,
because SQLite happily scans a table with three rows in it:

```
BEFORE  plan: SCAN forward_rules | CORRELATED SCALAR SUBQUERY 1 | SCAN json_each
BEFORE  p50: 2.366 ms   p95: 3.456 ms        (20,000 rules, per message)

AFTER   plan: SEARCH forward_rules USING INDEX idx_rules_source (source_chat_id=?)
              UNION ALL
              SEARCH forward_rules USING INDEX idx_rules_extra_sources (has_extra_sources=?)
AFTER   p50: 0.031 ms   p95: 0.038 ms        (same 20,000 rows, same query result)
```

**~71–76× faster on the hot path, per inbound message.** The cause was the shape
of the query, not the absence of an index: no index can answer
`source_chat_id = ? OR EXISTS(json_each(...))`, so *every* message read *every*
rule in the system. Two changes fixed it:

* the query is two branches over `UNION ALL` — an index seek for the primary
  source, and the JSON branch guarded by `has_extra_sources`, a column SQLite
  derives from `detection_criteria` itself, covered by a **partial** index that
  only contains the rows that really do watch extra sources (normally none).
  Derived, so it cannot drift and needs no backfill.
* the owner-scoped page no longer sorts through a temp B-tree: a composite
  `(owner_user_id, created_at)` index matches that read exactly. The
  single-column index it subsumes is dropped, so write cost does not grow.

Both queries return the same row for a chat that is an *extra* source of a rule
(`r1000-0`), which is the behaviour the rewrite had to preserve. Guards for all
of this — plan assertions, duplicate-match, unparseable criteria, paging,
upgrade of an existing database, idempotent re-open — are in
`core/tests/test_scale_indexes.py`.

## What this does not cover

Named rather than glossed, because a benchmark that only supports the claim is
not evidence:

* **Not Telegram.** The client pool is a stub, so nothing here measures MTProto
  throughput, flood-wait handling or Telegram's own rate limits. What is
  measured is the control plane: rule resolution, transforms, queueing, storage.
* **Not the delivery worker.** 5,000 jobs were queued and deliberately left
  undrained; draining them needs the real client and a real channel.
* **Not a Postgres cluster.** The Postgres path is covered by unit tests; no
  live cluster was available on this host, so no Postgres numbers are claimed.
* **Not 10,000 concurrent connections.** Concurrency here is 24 in-process
  asyncio workers. The per-message statement counts and the flat per-call
  latencies say how the *work* scales; they say nothing about socket counts,
  which is the HTTP/gRPC layer's problem.
* **One process, one SQLite file.** The 1,894 msg/s figure is a single-process
  write ceiling on a 2-vCPU host, not a claim about forwarded messages per
  second, which Telegram's limits bound long before this does.
* **Synthetic configuration.** Two tasks per tenant, text-only messages, one in
  200 tasks watching an extra source. A tenant with 500 tasks and heavy
  per-task settings is a different shape and has not been measured.
* **No soak.** Nothing ran for hours, so slow leaks and growth over time are
  not covered.
