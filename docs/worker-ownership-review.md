# Go Worker — ownership & resolution review (0 → 100)

A full pass over how the Go worker takes **ownership** of a job (claim,
lease, fence, tenant scoping) and **resolves** it (sent / failed / unknown /
retried, dead-letter, reconciliation), against the standards the skills
demand (golang-grpc, golang-security, golang-testing, golang-naming,
golang-lint, golang-cli). Scope: `cmd/worker`, `internal/queue`,
`internal/engine/{delivery,reconciler}`, `internal/rulestore`,
`internal/telegraph`, `internal/infra/aiprovider`, `internal/ops`,
`test/ownership`.

**Verification battery run for this review:** `go build ./...`,
`go vet ./...`, full `go test ./... -count=1` (green), `-race` on
`internal/queue`, `internal/engine/...`, `internal/ops` and
`test/ownership` (green), gofmt (the 24 pre-existing alignment-drift files
are untouched upstream drift, not this branch's).

---

## 1. Ownership model — how a job is owned

### 1.1 Claim ownership (queue lane) — **solid**

`ClaimBatch` (both stores) is a two-phase transactional claim: a bounded
candidate window ordered by the pinned source tuple
`(source_chat_id, source_message_id, created_at, id)`, a per-candidate
log-n probe for an earlier unsent same-target job (the per-target lane),
then one atomic `UPDATE … SET status='CLAIMED', worker_id=?, lease_until=?`
under the claim transaction. Ordering regressions and their history are
documented in place. Expired `CLAIMED` rows are re-claimable
(`lease_until < now`) — crash recovery — while the fence makes that
re-claim safe (§1.2). `PENDING/RETRY_WAIT/CLAIMED(expired)` plus
`next_retry_at <= now` is the complete claimable set; nothing else leaks
in. No worker-id check on re-claim is deliberate: one process shares one
worker id, and cross-process steals are exactly the expired-lease recovery
path.

### 1.2 Send ownership (the idempotency fence) — **solid**

`FenceClaim` reads the attempt row **before** any write (the pre-read is
what distinguishes a legitimate FAILED re-claim from a live same-worker
claim), then: first claim → INSERT + `FenceDispatch`; `SENT` →
`FenceAlreadySent` (terminal); `UNKNOWN` → `FenceUnknown` (never dispatched
blindly); `CLAIMED/SENDING` → `FenceInFlight` (live lease) / `FenceStale`
(expired) — **never stolen in either case**; `FAILED` → guarded re-claim
with `attempts+1`. `FenceRecordSent` is terminal (`status <> 'SENT'` guard)
and written before the job-row commit. PostgreSQL parity is
test-pinned (`TestPostgresConcurrentFenceClaimSingleWinner`,
`TestPostgresDuplicateFenceClaimSameWorkerNeverRedispatchesLiveLease`), and
the `test/ownership` harness pins the whole matrix
(`ATF-OWN-XONCE-001…102`, `ATF-OWN-ISO-001…006`, `ATF-OWN-MKR-001…010`)
under `-race`. The engine holds the fence lease at **2× the claim lease**
(`leaseUntil = now + 2*LeaseDuration`), so a live dispatch never looks
stale to its own fence.

### 1.3 Tenant ownership (owner_user_id) — **solid**

* Filters: read by id but **refused when `owner_user_id` is neither 0
  (pre-ownership row) nor the task's owner** — the delivery proceeds
  unfiltered and logs, mirroring the Python pipeline (fail-open with
  visibility, never cross-tenant application).
* AI configs: `Transformer.Transform` resolves primary **and** secondary
  configs through the owner-scoped store; a foreign id is refused, never
  billed.
* AI prompts: the worker's prompt store is owner-scoped
  (`owner_user_id = ? OR is_system = 1`), and the new enable-flag probe
  degrades to "enabled" rather than erroring (§4, fix 1 follow-up).
* Bot tokens: ids travel as references; the raw token never enters the
  worker (runtime-side resolution), and per-job bot adapters are per-job
  objects so settings never leak across jobs.

---

## 2. Resolution model — how a job is resolved

`handleFailure` maps the shared failure taxonomy onto exactly one lifecycle
per class, and the mapping is the contract:

| Class | Fence | Job row | Attempt budget | Dead-letter |
| --- | --- | --- | --- | --- |
| Ambiguous (timeout, dropped conn) | `UNKNOWN` | `MarkUnknown` | never spent | no — reconciler/operator owns it |
| FloodWait | `FAILED` | `Defer(retry_after)` — authoritative, never shortened | **not spent** | no |
| Dependency unavailable (session/target/source/network) | `FAILED` | `Defer(10s)` | **not spent** | no |
| Auth failure | `FAILED` | `MarkFailed` | n/a | no (visible, loud) |
| Permanent Telegram refusal | `FAILED` | `MarkFailed` | n/a | no |
| Transient | `FAILED` | `MarkRetry` (exponential, capped) | spent | yes, at `max_attempts` (`DELIVERY_EXHAUSTED`) |

`Defer` never spends an attempt and is status-guarded
(`NOT IN ('SENT','FAILED')`); `MarkRetry` spends one and dead-letters at
exhaustion in the same transaction as the budget check. The reconciler
closes aged UNKNOWN fences **only on recorded evidence that the RPC never
executed** (`code = Unimplemented`, dial-phase `Error while dialing`) — a
bare `Unavailable` stays UNKNOWN, because a transport that dies mid-call
after Telegram accepted the upload reports the same code. `ReconcileUnknown`
moves fence + job atomically with status guards and rolls back on
half-reconciliation. Terminal fences age out on the 7-day job-retention
window (bounded, hourly, never touching UNKNOWN rows — evidence is never
swept).

---

## 3. gRPC & security review

* **Gateway client**: one reused `grpc.ClientConn` (HTTP/2 multiplexed),
  per-call deadline, empty-token refusal at construction, contract tag +
  correlation metadata only (no content, no credentials in metadata).
  `transportFailure` maps gRPC codes onto the shared taxonomy with the
  UNIMPLEMENTED/PERMISSION_DENIED/RESOURCE_EXHAUSTED distinctions the
  /ops ring needs.
* **Observability client**: bounded 4096-entry queue, retry/backoff,
  severity policy with security exemptions, secret-shaped keys and content
  fields redacted at the choke point; stdout carries the same redaction.
* **SQL**: every store query is parameterized; the only `fmt.Sprintf` uses
  build placeholder strings and pinned column lists (int constants), never
  user data.
* **Secrets**: AI provider API keys are AES-256-GCM ciphertext at rest
  (key derived from `ATF_MASTER_KEY`); the worker refuses to run the AI
  stage without a readable, decryptable config store rather than
  delivering rewrites silently.
* **Media staging**: per-job scratch paths (unique by construction) and a
  strict `[a-z0-9]`, ≤15-char extension normalizer so a hostile
  `file_name` suffix can never become a path surprise.

---

## 4. Findings fixed in this review

### Fix 1 — poison-pill containment (crash-class, High)

There was no `recover()` anywhere in the worker: one panicking payload
crashed the process, the restart re-claimed the **same** job and panicked
again — a crash loop a single bad payload could sustain forever, stalling
the queue. `Engine.Process` now contains the panic: CRITICAL log with the
stack and a dedicated `JOB_PANIC_CONTAINED` event code, the job returns to
the retry budget (`MarkRetry` → bounded, dead-letters at exhaustion), the
fence is deliberately untouched (a post-send panic leaves the fence's own
record; an unrecorded one ages out to UNKNOWN like every other
ambiguity), and the ops collector gained a **`panicked`** outcome counter
(never folded into Telegram `failed`) exposed through `/healthz`.
Regression-pinned in `panic_containment_test.go`: the poisoned job retries
with one spent attempt, a healthy job right after it still delivers SENT,
and the counters agree.

### Fix 2 — backoff overflow (High)

`backoffFor` computed `2^attempts` with one multiply:
`time.Duration(math.Pow(2, 99)) * time.Second` **overflows int64
nanoseconds and wraps** — the table test caught `backoffFor(99) == 1s`, i.e.
a job with a high attempt count would retry in a tight loop instead of
waiting out the 5-minute cap. Replaced with an overflow-free doubling loop
that returns the cap as soon as it is reached (`TestBackoffForIsBoundedExponential`
pins the table, including the 2¹² and overflow classes).

### Fix 3 (follow-through) — the two fixes above also close the loop on
`MarkRetry`'s unguarded job-row update: even if a panic landed after the
job row was marked SENT, the re-claim path closes it as
`FenceAlreadySent` from the fence's terminal record — the fence, not the
job row, is the source of truth, which is exactly the design intent.

---

## 5. Noted, deliberate, left alone

* `MarkSent`/`MarkRetry` have no worker-id guard: only the claiming worker
  ever dispatches, one process shares one worker id, and the fence is the
  correctness boundary — a guard there would be ceremony.
* `FenceRecordSent` stamps the caller's worker id over the original
  claimer's: cosmetic in the audit trail (single worker id per process).
* The runLoop's `tracker.Outcome("failed")` on a `Process` error also
  counts ambiguous branding outcomes: the health snapshot is
  informational (state is derived from claim failures, not outcome
  ratios), and the ops collector already classifies precisely.
* gofmt alignment drift in 24 untouched files: a newer gofmt's struct-alignment
  opinion; reformatting them in this PR would bury the review in noise.
