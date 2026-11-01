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
│   ├── queue/                   SQLite store: claim/lane/fence/mapping/DLQ
│   ├── telegraph/               the business-neutral Gateway port
│   │   ├── grpcclient/          the only place that knows protobuf exists
│   │   └── fakegateway/         deterministic, TEST-ONLY gateway
│   ├── config/                  ATF_* resolution + fail-closed validation
│   └── health/                  readiness vs liveness
├── go.mod / go.sum
└── README.md
```

## The decision tree (a port, not a redesign)

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

## Bounded resources

| Resource | Bound |
|---|---|
| Goroutines | `ATF_QUEUE_NUM_WORKERS` (default 4), fixed at startup |
| In-flight claims | one batch per worker (`ATF_QUEUE_BATCH_SIZE`), through a bounded channel — natural backpressure, no unbounded in-memory queue |
| Media memory | none — files are staged in the shared scratch volume, never buffered |
| Retries | exponential backoff capped at 5 minutes; `Defer` never spends an attempt |
| gRPC calls | bounded by `RPCTimeout`, context-cancellable |
| Fence rows | one per logical send, keyed deterministically |

## Graceful shutdown

SIGTERM/SIGINT stops the claimer first, then waits for in-flight deliveries
(bounded by a 30s deadline). A job whose delivery did not finish keeps its
CLAIMED state and is reclaimed by another worker once its lease expires —
the fence guarantees that re-claim cannot double-send.

## Health: alive ≠ ready

* `/healthz` (readiness) probes the delivery store. A worker whose database
  is unreachable is `503`, so nothing routes work to it.
* `/livez` (liveness) only requires the loop to be advancing.
* `/metrics` reports delivered/retried/failed/unknown counts and the active
  in-flight count.

The container's HEALTHCHECK uses the `atf-worker healthcheck` subcommand, so
no curl is needed in the image.
