# Architecture Decision Records

## ADR-1 — Go owns business execution; Python owns Telegram connectivity

**Status:** accepted (stage 1 implemented)

**Context.** The Python Core mixes business logic (routing, filtering,
retry, scheduling, queue semantics) with Telegram transport (MTProto,
sessions, media bytes) in one 2,200-line dispatch path. Every reliability
fix has to thread both concerns; the delivery loop cannot be reasoned about
independently of pyrogram.

**Decision.** Split by *concern*, not by module: a Go Worker owns the
business execution plane and a Python Telegram Runtime owns MTProto
execution, connected by a versioned gRPC contract. Go decides WHAT;
Python executes the Telegram-specific operation and reports WHAT Telegram
actually reported.

**Consequences.** Retry, idempotency, scheduling and queue semantics become
testable without a Telegram client. The Python side shrinks toward
connectivity. The contract is the new integration surface — it must stay
business-neutral (no generic Python invocation) or the boundary rots.

**Rejected.** Porting the whole pipeline to Go at once: it mixes
*ingest* (which is fine in Python and deeply integrated with the bot) with
*execution*, and a big-bang rewrite cannot be rolled back per capability.

## ADR-2 — The worker is initially ONE deployable application

**Status:** accepted

**Context.** The reference architecture names six engines (forward, copy,
filter, media, transform, delivery).

**Decision.** One Go binary with internal engine packages, not six
microservices. "Do not introduce a new microservice for every engine."

**Consequences.** No new broker, no inter-engine network hops, no distributed
transaction across engines, one deployment to operate. The engines remain
independently *testable* through explicit interfaces — which is what the
reference architecture actually needs. Splitting later is a capacity
decision, not an architectural one.

## ADR-3 — Strangler-fig: the queue is strangled, not replaced

**Status:** accepted (stage 1 implemented)

**Context.** A new queue means new tables, a migration, a dual-write and a
reconciliation problem — all for zero user-visible benefit.

**Decision.** Keep `delivery_jobs`, `delivery_attempts`, `message_map`,
`dead_letter_queue` exactly as they are. Move only **consumption** to Go,
behind `ATF_WORKER_ENABLED` (default off). Enqueue stays Python's in both
modes. One authoritative execution path at a time; rollback is a config
flip.

**Consequences.** Zero schema change, zero data migration, zero risk of a
second source of truth for durable jobs. The Python loop is the fallback for
everything until stage 4 deletes it — which is a separate PR with its own
evidence.

## ADR-4 — Idempotency is the fence; uncertain delivery is never retried blindly

**Status:** accepted (implemented and tested)

**Context.** Telegram has no idempotency key for ordinary sends. A lease
expiring mid-dispatch, or a crash between Telegram's accept and the commit,
leaves "did it send?" unanswerable.

**Decision.** Claim the logical send in `delivery_attempts` atomically
**before any Telegram call**, with a deterministic business key shared
byte-for-byte with Python. SENT and UNKNOWN rows are never re-dispatched.
Timeouts record `UNKNOWN`, never `FAILED`.

**Consequences.** At-most-once for a single logical send; at-least-once for
a send that never reached Telegram. **Exactly-once is explicitly not
claimed** — it is unobtainable without a Telegram-side key, and pretending
otherwise is how duplicates ship.

## ADR-5 — Media is staged through a shared scratch volume, never in memory

**Status:** accepted (implemented)

**Context.** A re-upload bypass needs the source file's bytes somewhere both
processes can reach. Buffering a whole video in either process is the
obvious failure mode.

**Decision.** The worker allocates a unique per-job path in the scratch
volume and tells the runtime to write/download there; uploads are staged
from that path. The worker owns the volume's lifecycle and cleanup.

**Consequences.** Neither process holds a full media file in memory; the
volume is bounded by `ATF_MEDIA_QUOTA_BYTES`; paths are unique by
construction (job ids). The cost is a shared volume — an explicit one,
documented in compose, rather than an implicit assumption that a path in one
container exists in another.

## ADR-6 — The filter engine is a parity port, not a redesign

**Status:** accepted (implemented, parity-tested)

**Context.** A task's filter must decide the same way in either engine, or
the same task forwards a different set of messages depending on which engine
picked the job up.

**Decision.** Port `FilterEngine` (including the legacy `regex_patterns`
compatibility rule and the Persian/Arabic variant-aware matcher) into Go,
and keep the Python original. The Go tests are the parity guard.

**Consequences.** Duplication — deliberate and documented. The alternative
(calling Python for each filter decision) would put a network hop on the
delivery hot path and leave the boundary meaning nothing.

## ADR-7 — Contract evolution: additive only, generated stubs committed, CI-verified

**Status:** accepted

**Context.** The gRPC contract is now a load-bearing interface between two
languages and two processes.

**Decision.** Add optional fields and new RPCs only; never renumber, never
change a type, never remove an RPC. `<<contract: tfgw/v1>>` is stamped into
the file and into every call's metadata; the health RPC reports major/minor
and the Go client refuses a runtime whose major version it does not speak.
The generated Go **and** Python stubs are committed, and CI diffs a fresh
`protoc` run against them.

**Consequences.** A stale regeneration fails CI instead of drifting in
production. A breaking change requires a version bump both sides can see.
