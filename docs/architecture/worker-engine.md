# Go Worker Engine

> Implemented: `worker/` — an independently buildable, independently
> deployable Go module (its own `go.mod`) that owns the delivery-job
> execution plane.

## Structure

```
worker/
├── cmd/worker/main.go          entrypoint: claim loop, shutdown, health
├── proto/
│   ├── telegram_gateway.proto   the tfgw/v1 contract (source of truth)
│   └── gen/                     committed Go + Python stubs (CI-verified)
├── internal/
│   ├── domain/
│   │   ├── job/                 DeliveryJob, Payload, AttemptKey, FenceOutcome
│   │   └── rule/                read-only ForwardRule/FilterRule projection
│   ├── engine/
│   │   ├── filter/              parity port of the Python FilterEngine
│   │   ├── transform/           target-scoped rewrite + length clamping
│   │   └── delivery/            the delivery decision tree + retry policy
│   ├── queue/                   SQLite + PostgreSQL stores: one Store interface
│   ├── rulestore/               composite rule store (forward_rules + filter_rules)
│   ├── parity/                  offline cross-engine parity harness
│   ├── telegraph/               the business-neutral Gateway port
│   │   ├── grpcclient/          the only place that knows protobuf exists
│   │   └── fakegateway/         deterministic, TEST-ONLY gateway
│   ├── config/                  ATF_* resolution + fail-closed validation
│   └── health/                  readiness vs liveness
├── go.mod / go.sum
└── README.md
```

## Storage: one interface, two backends

`worker/internal/queue.Store` is the contract; `sqlite.go` and `postgres.go`
are the two implementations. Business logic lives above the interface and is
not forked per dialect — only the SQL differs.

The backend is chosen the way the Python Core composition root chooses it:
`ATF_DATABASE_URL` selects the delivery store, `ATF_POSTGRES_ALL` selects
`forward_rules`, and `filter_rules` is always SQLite. See
`data-ownership.md` for the full matrix and the two-database case.

Startup validation fails closed: `NewPostgres` pings the database and runs
`SchemaCheck` (table + column presence against the production schema) before
the worker reports ready. A store pointed at the wrong database — or a
database the Core has not bootstrapped — refuses to start rather than
looking healthy while doing nothing.

## The decision tree (a port, not a redesigned)

`delivery.dispatch` mirrors `DurableQueueManager._dispatch_to_telegram`
in order:

1. **keyword reply** → pure text, never the source's media;
2. **branding hop** → forward C's post (or a clean copy of it when the
   forward is restricted);
3. **DIRECT_FORWARD** → native forward, falling through to the copy path on
   a restricted source;
4. **album** → `album` | `first` | `split`, where `split` **resumes** from
   the per-item confirmations persisted in the job payload (audit finding
   E2E-5);
5. **content modes** → TEXT_ONLY / MEDIA_ONLY / AUTO-TEXT_AND_MEDIA;
6. plain text send.

Every restricted-source failure may trigger the copy → re-upload bypass,
**only when the task opted in** (`fallback_enabled`) — a refused forward
must fail the job instead of being rescued by a bypass the owner did not
ask for (test: `TestFallbackOptOutIsRespected`).

## Delivery lifecycle: preflight before the fence

The engine separates a job's lifecycle into a **preflight** stage and a
**dispatch** stage:

```
CLAIM JOB → READ AUTHORITATIVE RULE → ACTIVE/PAUSED STATE →
EVALUATE ATTACHED FILTER → TARGET RATE LIMIT / SESSION READINESS →
CLAIM SEND-SIDE FENCE → EXECUTE TELEGRAM SIDE EFFECT →
RECORD OUTCOME → COMMIT DELIVERY STATE
```

The fence claim is the **last** step before the first Telegram side effect,
on purpose. An earlier implementation claimed it first and then ran the
checks; a job whose rule was paused deferred *while holding a claimed
fence*, and the sweeper eventually turned that claim into UNKNOWN — a job
blocked forever with no send ever having happened. The regression suite in
`fence_ordering_test.go` pins the corrected ordering: a paused rule, a
rule-store outage, a filter rejection and a rate-limited target all defer
**without** leaving a fence claim behind, while a genuine send still
requires one.

## Bounded resources

| Resource | Bound |
|---|---|
| Goroutines | `ATF_QUEUE_NUM_WORKERS` (default 4), fixed at startup |
| In-flight claims | one batch per worker (`ATF_QUEUE_BATCH_SIZE`), through a bounded channel — natural backpressure, no unbounded in-memory queue |
| PostgreSQL connections | pool of 16 (+8 idle), 30min lifetime / 5min idle lifetime |
| Media memory | none — files are staged in the shared scratch volume, never buffered |
| Retries | exponential backoff capped at 5 minutes; `Defer` never spends an attempt |
| gRPC calls | bounded by `RPCTimeout`, context-cancellable |
| Fence rows | one per logical send, keyed deterministically |

## Graceful shutdown

SIGTERM/SIGINT stops the claimer first, then waits for in-flight deliveries
(bounded by a 30s deadline). A job whose delivery did not finish keeps its
CLAIMED state and is reclaimed by another worker once its lease expires —
the fence guarantees that re-claim cannot double-send. The rule store and
the delivery store are closed in that order; each database handle is closed
exactly once even when both readers shared one SQLite file.

## Health: alive ≠ ready

* `/healthz` (readiness) probes the delivery store. A worker whose database
  is unreachable is `503`, so nothing routes work to it.
* `/livez` (liveness) only requires the loop to be advancing.
* `/metrics` reports delivered/retried/failed/unknown counts and the active
  in-flight count.

The container's HEALTHCHECK uses the `atf-worker healthcheck` subcommand, so
no curl is needed in the image.

## Rule store: read-only, never an editing surface

`worker/internal/rulestore` reads `forward_rules` and the `filter_rules` row
it references, from whichever backend owns each. It is strictly read-only:
the worker is not an alternative API for editing users' rules. Errors are
reported as errors — a database outage is never modeled as "rule deleted",
because a worker that made that conversion would fail deliveries en masse
during an outage that should have deferred them. A dangling filter
reference proceeds **unfiltered** (the Python pipeline's own behavior), and
a pre-v30 row missing `regex_block`/`regex_require`/`is_enabled` keeps
working with its filter still **enabled** — the Python repository's own
compatibility rule.
