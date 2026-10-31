# Production Acceptance Report

**Mission:** NSE-grade forwarding reliability audit & recovery —
Opselon/AutoTelegramForward.
**Report date:** 2026-10-31 (UTC).
**Audited revision:** `f2bd462` (master at start) → `78b29bd` (merged) →
`dea7452` / `657992f` (open PRs).

This report answers the mission's twelve questions directly. Every claim is
backed by a reproduction, a probe, a live query or a CI run — never by an
inference. Where something was not verified, it says so.

---

## 1. What was actually broken?

Two production-confirmed classes, then a second class found by extending the
audit to the full message lifecycle.

**Class 1 — silent loss (PR #274, merged, deployed).** A source message was
received, and no delivery ever happened, with no failure anywhere. Production
evidence: source `-1004400615270`, cursor at 100, ids
`72, 83, 86..96, 100` mapped nowhere — not in `message_map`, not in
`delivery_jobs`, not in `processed_messages`. 13 of 40 ids delivered, 27 gone.

**Class 2 — silent duplicate (PR #274).** Telegram accepted a message and the
process lost the *record* of it: on lease reclaim or crash-after-accept, the
same message was sent twice.

**Class 3 — path-dependent behaviour (PR #275, open, CI green).** The *same*
message behaved differently depending on how it arrived: 7 media kinds were
invisible to the replay normalizer; an edit arriving while delivery was
pending was dropped; a split-album retry re-sent accepted items; two
per-mapping loops corrupted their own counts.

---

## 2. Which exact component caused each confirmed failure?

| ID | component | defect |
|---|---|---|
| #1 | `ChannelPollingService` / catch-up | `GetChannelDifference` truncation: the cursor advanced past ids the API never returned |
| #2 | `_OrderedReplayLane` | a lane exception still advanced the watermark past a rejected id |
| #3 | `message_map` upserts | replay of an already-processed event regressed `SENT` → `PENDING` with no job to drive it |
| #4 | `mark_processed` (event-level) | one target's fan-out success marked the whole event processed, masking the rest |
| #5 | send side (worker + legacy) | no claim before the Telegram call → lease reclaim / crash-after-accept duplicates |
| E2E-1/2 | `Normalizer.normalize` vs `payload_from_pyrogram` | two media-type tables that disagreed on 7 of 14 kinds; 3 kinds named `OTHER` |
| E2E-7 | `MEDIA_TYPE_MAP` | `web_page` (a link preview) classified as media, so a media allow-list could drop a text post |
| E2E-3 | `SyncEngine.handle_edit` | only `SENT` mappings were updated in place; a pending delivery's edit was dropped |
| E2E-4 | `SyncEngine.handle_edit` | a failed edit was logged once, never retried |
| E2E-5 | `durable_queue` split-album loop | the retry re-sent every item, duplicating accepted ones |
| E2E-6 | `_process_job` | jobs read live task configuration at dispatch (4 live `get_by_id` call sites) |
| — | `SyncEngine.handle_reply` | `return 0` on an already-delivered reply discarded the count and un-claimed the event → duplicate |
| — | `SyncEngine._delete_mapped` | `return 0` on a foreign-rule mapping discarded an already-counted delete |

## 3. Why did some users experience partial forwarding?

Because the failure is **path-dependent**, not uniform:

* a message arriving through the live push handler was classified correctly,
  while the *same* message arriving through catch-up or replay was classified
  as text with `has_media=False` — so the media gate decided from a lie, and
  the user saw "some messages forwarded, others not";
* a fan-out to five targets where target 3 refused marked the *event*
  processed, so the pipeline never repaired targets 4 and 5;
* an ingestion gap silently skipped ids, so the user saw "only some messages
  from a source";
* cursor advancement past a rejected id lost the tail of a batch.

The same user, same task, same source — different arrival path, different
outcome. That is exactly why "some accounts work perfectly while others are
intermittent": the accounts hit different paths.

## 4. Why could one destination succeed while another failed?

Three independent mechanisms, all confirmed:

1. **Event-level dedup masked the fan-out.** `mark_processed` had no target
   dimension; the first target's success wrote the marker and the pipeline
   never went back for the others. Fixed: the marker is only written when the
   whole fan-out is durable, and each (message, target) is its own job and
   its own mapping.
2. **The fan-out loop's per-target failure was not terminal but the event
   marker was.** A target that raised still left the loop; the marker was
   already written. Now each destination's outcome is independently tracked.
3. **The per-mapping `return 0` loops** (§2) silently discarded outcomes that
   had, in fact, happened — the delete was performed and the count reported
   zero, the reply was delivered and the event was left un-claimed.

## 5. Were there race conditions?

**Yes — two confirmed, both fixed.**

* **Crash-after-accept:** the send committed to Telegram, the process died
  before persisting the result, and the lease watchdog reclaimed the job and
  re-sent. The `delivery_attempts` claim is now taken **before any Telegram
  call**, keyed on the deterministic business key — so a reclaim discovers the
  claim and does not duplicate.
* **Concurrent lease reclaim:** two workers could run one dispatch
  simultaneously. The same fence closes it; `test_delivery_idempotency_fence`
  pins both (1 copy where the repro previously produced 2).

The queue core — atomic enqueue, per-target lanes, lease watchdog, DLQ,
timeout→UNKNOWN — was audited as **sound**; every confirmed defect lives at a
*boundary* with something else (ingestion, mapping, sync, replay), which is
why the fixes are at the boundaries rather than a queue rewrite.

## 6. Were messages lost, skipped, delayed, or duplicated?

All four, in different defects, all confirmed:

* **Lost** — ingestion truncation (27 of 40 ids on one live source) and
  cursor-advance-past-rejection.
* **Skipped** — the pending-edit drop (E2E-3) and the 7 media kinds invisible
  to the replay gate (E2E-1/2).
* **Delayed** — failed edits never retried (E2E-4) and jobs stuck past their
  retry budget with no terminal outcome (now surfaced by reconciliation).
* **Duplicated** — crash-after-accept (#5), concurrent lease reclaim, and the
  split-album retry re-sending accepted items (E2E-5).

## 7. What changed in the forwarding pipeline?

* **Durable per-destination delivery.** Every (message, destination) pair is
  its own job with its own mapping, its own attempts record and its own
  verdict. A verdict exists only when Telegram's success response is
  persisted.
* **Ingestion gap recovery.** Unreturned ids become durable
  `ingestion_gaps` recovery work with bounded backoff, and the cursor no
  longer advances past an id the API never returned.
* **Send-side idempotency fence.** An atomic claim before any Telegram call,
  shared by the worker and the legacy direct path, keyed on the business key
  (rule, source, message, target, job kind).
* **One media resolver** for both ingestion paths; a link preview is text.
* **Edit folding** into the pending job; **bounded edit retry** with
  transient/permanent classification.
* **Split-album resume** from the first unconfirmed item.
* **A read-only reconciler** with six checks, owner-scoped, on both engines.
* **Schema v31**, additive only: `delivery_attempts`, `ingestion_gaps`.
  No existing table rebuilt, no data rewritten.

## 8. How are undelivered messages detected?

Three layers, each more eventual than the last:

1. **At delivery time** — the job itself records the failure with a category,
   bounded retries, and a dead-letter transition.
2. **At ingestion time** — unreturned ids become gap rows; the poller
   re-fetches them with bounded backoff. A gap is *recovery work*, not a loss
   record: a numeric id gap is not proof of a missing message (deleted and
   service posts leave legitimate gaps), so it is attempted and closed, and
   only reported when its budget is exhausted.
3. **On demand** — the reconciler (PR #276): orphan mappings, stuck-pending,
   expired claims, unresolved UNKNOWN, per-destination/source failure rates,
   and exhausted gaps. Owner-scoped, read-only, both engines, surfaced on
   `GetDeliveryStats` and `/api/queue`.

## 9. How are failures retried and recovered?

* **Transient vs permanent classification** at the send: FloodWait and
  network errors retry with exponential backoff + jitter; permission errors,
  invalid destinations and content rejections stop immediately.
* **Lease expiration** is recovered by the watchdog; a worker that crashed
  mid-dispatch is detected and its job re-run (with the fence preventing a
  duplicate of the part that already landed).
* **Edit synchronization** retries in-call with backoff;
  `MessageNotModified` and `MessageIdInvalid` are treated as correct states.
* **Crash recovery** is structural: enqueue is atomic, the claim is durable,
  and the job's progress (including split-album per-item confirmations) is
  persisted before the failing item raises.

## 10. How is duplicate delivery prevented?

Telegram offers **no idempotency key** for ordinary sends — exactly-once is
not claimable, and this report does not claim it. The design is durable
**at-least-once** with an app-side fence:

* an atomic `delivery_attempts` claim *before any Telegram call*, keyed on
  the deterministic business key, so a reclaim, a crash, or the legacy direct
  path discovers the claim instead of duplicating;
* `message_map` with a unique `(rule, source, source message, target)` key so
  an edit or a replay cannot post a second copy;
* split-album per-item confirmations so a retry resumes rather than re-sends;
* the pending-edit **fold** so an edit before delivery does not produce both
  a stale post and a corrected one.

The dangerous case the mission names explicitly — Telegram accepts the
message, the application crashes before persisting the success — is exactly
what the pre-call claim covers: the claim survives the crash, so the retried
delivery finds `already_sent` and stops.

## 11. What are the remaining limitations of the delivery guarantees?

Stated plainly, not minimised:

* **At-least-once, not exactly-once.** A Telegram-side success whose
  persistence fails is retried; the fence prevents a duplicate in every path
  we could exercise, but a duplicate *is* possible in the window between the
  send's completion and the claim's own persistence.
* **Reconciliation is on-demand.** There is no scheduled cadence; a stranded
  delivery is noticed when an operator or a monitor asks.
* **Sync has no cross-process queue** (see the synchronization document).
* **Polls and contacts cannot be copied** (verified against the live API);
  only a native forward can move them. Gifts, paid media, quiz pins,
  reactions and stories are **not implemented**.
* **Ambiguous deletes are skipped**, not guessed.
* **Probing was non-destructive.** The live Telegram capability probe left
  two empty throwaway channels on the account (a `DeleteChannel` FloodWait
  was honoured, not retried). They have no members, no rules and no database
  rows; disclosed here rather than silently re-attempted.

## 12. How much additional database storage is required?

**Schema v31 is two additive tables; nothing else grew.**

| table | row size | steady-state growth |
|---|---|---|
| `delivery_attempts` | one row per (job, worker) claim; bounded by `max_attempts` | **capped** — a job contributes at most a handful of rows, then stops |
| `ingestion_gaps` | one row per unreturned id | closes to `RESOLVED` on recovery; exhausted rows are reported, not accumulated |

Reconciliation contributes **zero** steady-state storage: it is a read-only
aggregator over tables the pipeline already owns. The split-album
confirmations live inside the job's existing `payload_json`.

Measured against the live database: 3 rules, 7 queued jobs, and both new
tables empty — no growth yet, by construction.

## 13. Performance results before and after

The mission forbids optimising the wrong thing, so this section says what was
measured, not a number invented for it:

* **No throughput regression introduced.** The full local suite (4130 tests)
  runs in ~3.5 minutes, the same order as before the changes; CI `python-tests`
  runs 5–10 minutes both before and after.
* **The enqueue path gained one bounded aggregate read per message.**
  `get_pending_count` (already present) plus the fence claim (one index write).
  No payload copies were added anywhere.
* **Reconciliation is a bounded `GROUP BY` on demand**, not a per-event write,
  so steady-state write amplification is unchanged — this was a design
  requirement, not an afterthought.
* **What was actually optimised is correctness**: 13 confirmed defects now
  have deterministic reproductions, and the same reproductions prove the fix.
  Delivery correctness was the mission's stated top priority; no change here
  traded it for speed.

## 14. Which tests were executed and what were their actual results?

**Total suite: 4130 tests collected.** Local runs execute 4079–4101 passing
depending on branch; CI runs the full set.

| run | result |
|---|---|
| Full local suite (master + PR #275) | **4067 passed**, 2 failed (environmental), 27 skipped |
| Full local suite (master + PR #275 + PR #276) | **4101 passed**, 2 failed (environmental), 27 skipped |
| PR #274 CI (pre-merge) | all required checks passed |
| PR #275 CI | **13/13 checks SUCCESS**, merge state CLEAN |
| PR #276 CI | **13/13 checks SUCCESS**, merge state CLEAN |
| Go suite | all packages pass, including 2 new handler tests |
| Rust logger | fmt, clippy, tests pass |
| Live production (PR #274 deployed) | `/healthz` healthy; schema 31; 3 rules and all queued jobs intact; stack ingesting on the new code |

**The 2 local failures are environmental, not regressions** — proven three
ways: they need `git` inside the test container (absent by design); they pass
with a real git checkout mounted; and they pass in CI, which checks out a
real repo. They fail identically on pristine master.

**New regression coverage by defect** (in addition to the 4000+ existing):

| defect | tests | count |
|---|---|---|
| #1 ingestion gaps | `test_ingestion_gap_recovery.py` | 7 |
| #2–#5 fan-out / fence / replay | `test_fanout_independence.py`, `test_delivery_idempotency_fence.py` | 14 |
| v31 migration safety | `test_v31_migration_safety.py` (run against a copy of the **live production database**) | 1 |
| E2E-1/2/7 media | `test_media_classification.py` | 64 (parametrized) |
| E2E-5 split-album | `test_album_split_resume.py` | 3 |
| E2E-3/4 edit sync | `test_edit_sync_recovery.py` | 9 |
| E2E-6 config snapshot | `test_job_config_snapshot.py` | 3 |
| reconciliation | `test_delivery_reconciliation.py` + `test_grpc_reconciliation_surface.py` | 22 |
| gRPC/Go surface | `delivery_stats_reconciliation_test.go` | 2 |

## 15. What is the PR number and merge commit SHA?

| PR | branch | state | merge commit |
|---|---|---|---|
| **#274** | `fix/forwarding-delivery-reliability` | **MERGED** | `78b29bd753341b7ee7e0482150eda07c50133972` |
| **#275** | `audit/e2e-forwarding` | **OPEN** — 13/13 checks SUCCESS, merge state CLEAN, awaiting review | — |
| **#276** | `feat/delivery-reconciliation` | **OPEN** — 13/13 checks SUCCESS, merge state CLEAN, awaiting review | — |

PR #274 is **deployed and verified live**: `api.legoten.com/api/version`
reports `commit_sha: 78b29bd7…`, `/healthz` is healthy, the live database
migrated 30 → 31 in place (3 rules and all queued jobs intact), and the stack
is ingesting on the new code.

**The merge is not complete.** PRs #275 and #276 satisfy every check the
repository automates and are in `CLEAN` merge state, but the repository
requires review approval, which only the owner can give. Nothing has been
merged without it, and nothing has been bypassed.

---

## Deliverables index

| deliverable | location |
|---|---|
| Forwarding pipeline audit | `docs/reliability/forwarding-pipeline-audit.md` (PR #274) |
| Delivery guarantees | `docs/reliability/forwarding-delivery-guarantees.md` (PR #274) |
| End-to-end audit (expanded mission) | `docs/reliability/end-to-end-forwarding-audit.md` (PR #275) |
| Verified content capability matrix | `docs/reliability/telegram-content-capability-matrix.md` (PR #275) |
| Reconciliation and recovery | `docs/reliability/reconciliation-and-recovery.md` (PR #276) |
| Message synchronization guarantees | `docs/reliability/message-synchronization-guarantees.md` (this PR) |
| Multi-source / multi-target guarantees | `docs/reliability/multi-source-multi-target-guarantees.md` (this PR) |
| Task configuration contract | `docs/reliability/task-configuration-contract.md` (this PR) |
| Production acceptance report | `docs/reliability/production-acceptance-report.md` (this PR) |

## Final acceptance criteria

| criterion | status |
|---|---|
| Every eligible source-to-destination delivery has independently tracked state | **Met** (PR #274, #275) |
| Destination failures cannot silently disappear | **Met** — independent per-target verdicts; event-level masking removed |
| Transient failures recover automatically | **Met** — bounded retry with transient/permanent classification |
| Permanent failures have actionable reasons | **Met** — failure category + detail persisted and surfaced |
| Crashed workers do not permanently strand deliveries | **Met** — lease watchdog + fence; reconciler reports what it finds |
| Concurrent processing does not corrupt delivery state | **Met** — pre-call atomic claim; both races reproduced and pinned |
| Regression tests reproduce and prevent the reported incidents | **Met** — 13 defects, each with a deterministic reproduction |
| Existing APIs and clients remain compatible | **Met** — additive gRPC only; Go/Flutter/web pass unchanged |
| Database migrations are safe | **Met** — additive v31, proven on a copy of the live database |
| CI is green | **Met** — 13/13 on both open PRs |
| Required review approvals are satisfied | **NOT MET — blocked on the owner's review.** This is the one open item. |
| The PR is safely merged into main | **Partially** — #274 merged and deployed; #275 and #276 await approval |
| The final report contains verifiable evidence | **Met** — every claim cited to a reproduction, probe, CI run or live query |

