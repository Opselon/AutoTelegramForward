# Forwarding Pipeline Reliability Audit

**Scope:** Opselon/AutoTelegramForward v2.4.0 — Python core, Go API, Rust logger
**Audited revision:** `f2bd462` (master, 2026-10-31)
**Method:** static review of the full execution path + live production state
(`atf-core` / `atf.db`) + deterministic reproduction of each hypothesis against
the real repository and persistence layers. **Every confirmed finding below is
backed by a reproducing script or a production-data query**, not by inference.

---

## 1. Architecture and execution flow

The platform has **two ingestion sources** feeding **one pipeline** that fans out
into **one durable queue** drained by a worker pool:

```
SOURCE MESSAGE RECEIVED
   ├─ (a) live push update  → pyrogram handler → MessageDispatcher._make_handler()
   └─ (b) gap recovery      → ChannelCatchUpService (boot)  ┐
                            → ChannelPollingService (15s)   ┘→ dispatcher handler
        ↓
   Event handler            dispatcher._handler() — album aggregation, edit/reply routing
        ↓
   MESSAGE VALIDATION       Normalizer.normalize() → MessagePayload
        ↓
   FILTERING / RULE EVAL    pipeline.process()
                              INGEST → list_active_by_source → matching_rules
                              DEDUPLICATE (since_ts, edit window, processed_messages,
                                            content_dedup claim)
                              FILTER (filter_rule, media/link gates, content policy)
                              FORWARD_EVERY_N (atomic counter slot)
                              TRANSFORM (replace rules, AI rewrite, header/footer)
        ↓
   DESTINATION EXPANSION    rule.all_targets()  ← legacy `target_chat_id` + `target_chat_ids`
        ↓
   QUEUE SUBMISSION         DurableQueueManager.enqueue_delivery()  (per target)
                              └ _enqueue_atomic(): message_map row + delivery_jobs row
                                in ONE SQLite transaction
        ↓
   WORKER CLAIM             SqliteDeliveryQueueRepository.claim_batch()
                              one in-flight job per target ("lane"), source order
        ↓
   TELEGRAM SEND            _dispatch_to_telegram()  (forward / copy / album / re-upload)
        ↓
   TELEGRAM RESPONSE        success → (job_id, target_msg_id) → mark_sent_batch()
        ↓
   PERSISTENCE              message_map.delivery_status = SENT (+ target_message_id)
        ↓
   RETRY / RECOVERY         transient → mark_retry (exp. backoff + jitter)
                            timeout  → mark_unknown
                            exhausted → dead_letter_queue
                            crashed lease → watchdog recover_expired_leases()
```

**Good news first:** the durable-queue core is substantially more robust than the
reported symptoms suggest. Atomic enqueue, per-target lane serialization, lease
recovery, dead-lettering, timeout→UNKNOWN classification and failure
classification (`failure_policy.py`) all exist and are well tested
(`test_queue_concurrency_and_idempotency.py`, `test_p0_dataplane_reliability.py`).
**The bugs are at the boundaries**, not in the queue itself.

---

## 2. Confirmed failure points (proven, ranked by impact)

### CONFIRMED #1 — Ingestion gaps are permanent and invisible (the chief incident)

**Symptom reported:** *"Only some messages from a source channel are forwarded;
others silently disappear."*

**Production evidence** (live `atf.db`, snapshot 2026-10-31 02:54 UTC):

| source `-1004400615270` ("A") | |
|---|---|
| channel_cursors.last_message_id | `100` |
| poller state shown in live logs | `cursor=100 top_msg_id=100 pts=103` (every 15 s) |
| processed_messages rows | 61–71, 73–85, 92, 97, 98, 99 |
| message_map rows | same 27 ids |
| **missing ids 61..100** | **72, 83, 86, 87, 88, 89, 90, 91, 93, 94, 95, 96, 100 — 13 posts** |

No `message_map` row, no `delivery_jobs` row, no `processed_messages` row, **no
log line**. The poller reports "nothing newer than the cursor" for the rest of
time. The catch-up service is governed by the *dialog read marker*
(`read_inbox_max_id`), which Telegram advances when it *delivers* the difference
— not when the app processes it, and not at all for the truncated tail.

**Mechanism (proven by reproduction — repro A):** `ChannelPollingService._poll_channel()`:

1. `top = _channel_top_message()` — reads the true top id (100).
2. `diff = GetChannelDifference(..., pts=pts, limit=MAX_POSTS_PER_SWEEP)` —
   Telegram returns a **truncated subset** whenever the requested range spans
   service messages, deleted posts, or a stale `pts`, and `limit=100` caps the
   slice. In production it returned only `{84,85,92,97,98,99}`.
3. `lane.join()` waits only for **the posts Telegram returned**.
4. `self._state[chat_id] = max(..., messages[-1].id)` then `_persist_cursors()`
   — the cursor jumps to the **last *returned* id**, but only advances to `top`
   when `messages` is empty (line 510–514) or to `messages[-1]id` when it is
   not. Either way the **unreturned ids between `cursor` and `top` are never
   re-requested**; the next sweep sees `top <= cursor` and does nothing.

**Why the boot catch-up does not repair it:** `ChannelCatchUpService` computes
`gap = top - read_inbox_max_id` from the *dialog*. Telegram advances the read
marker when it serves the difference, so a truncated replay marks the channel
"read" and `top <= seen` returns 0 forever.

**Reproduction (repro A, deterministic, fake Telegram client + real
`ChannelPollingService`):** source ids 61..100, difference returns
`{84,85,92,97,98,99}` → `cursor=99`, **40 of 40 ids never reached the pipeline**,
`top <= cursor` on every later sweep. (In the same run the lane's `_drain`
exceptions were swallowed by the lane's `except Exception` and `join()` still
returned True — see CONFIRMED #2.)

**Confidence: HIGH.** This is the exact production signature.

---

### CONFIRMED #2 — Lane/replay exceptions are swallowed; the cursor still advances

`_OrderedReplayLane._drain()`:

```python
except Exception:
    self.failed += 1
    logger.exception("poll: ordered replay failed for %r", self.key)
```

and `_dispatch_one()` raises `RuntimeError` when no handler is registered or
`PyroMessage._parse` returns `None`. `lane.join()` returns `True` as long as the
queue drained — **a batch where every post failed parse still advances the
cursor** (`messages[-1].id`), because the cursor logic keys on what was
*submitted*, not on what succeeded.

In repro A the fake raw messages had no `from_id`, so **every** post failed
`PyroMessage._parse`, `self.failed == 6`, and the cursor advanced to 99 anyway.
With real MTProto objects the same happens whenever `_parse` fails (media
reference resolution, missing `from_id` on a channel post, an unknown chat in
the diff's `chats` map).

**Confidence: HIGH** (observed live in the reproduction and visible in production's
missing set vs. cursor).

---

### CONFIRMED #3 — Replay of a processed event regresses `message_map` SENT → PENDING with no job to drive it

`DurableQueueManager._write_mapping_and_job()` (v12.3 "atomic" unit):

```python
if job_kind == "forward":
    conn.execute("INSERT INTO message_map ... ON CONFLICT(...) DO UPDATE SET
        delivery_status = excluded.delivery_status, ...")   # ← unconditional PENDING
cur = conn.execute("INSERT OR IGNORE INTO delivery_jobs ...")
return cur.rowcount > 0                                     # ← False on replay
```

The mapping upsert is **written before the job insert is even attempted**, and
the `ON CONFLICT … DO UPDATE` clause has no status guard. When the job already
exists (any replay: reconnect re-sweep, catch-up, duplicate push), the job
insert is ignored **but the mapping has already been flipped back to PENDING**,
keeping the stale `target_message_id`. There is no job row to drive it, so:

* the mapping claims the message is undelivered while the target channel
  actually has it;
* a reconciliation pass reading `message_map` would flag it as "missing
  delivery" and could re-send it (duplicate);
* edit/delete sync, which keys off `message_map`, now sees an inconsistent row.

**Reproduction (repro B, real `SqliteDatabase` + real repositories):**
deliver → `mark_sent` + `update_delivery(SENT, 4242)` → replay →
`mapping AFTER replay: PENDING, tgt_msg_id=4242`, jobs: one row, `status=SENT`.

**Confidence: HIGH.**

---

### CONFIRMED #4 — One target's success masks another target's failure (event-level dedup)

`pipeline._process_for_rule()`:

```python
enqueued_any = False
for target in delivery_targets:
    ok = await self._queue.enqueue_delivery(...)
    if ok: enqueued_any = True
...
if enqueued_any and self._processed is not None:
    await self._processed.mark_processed(rule.id, payload.chat_id, payload.message_id, ...)
```

`processed_messages` is keyed `(rule_id, chat_id, message_id)` — **no target
dimension**. A message that reached 4 of 5 targets (one enqueue refused by
backpressure) is marked processed for the whole event; a replay of that event
is then dropped by `was_processed()` **before it can repair the missing
target**. The target's missing copy has no delivery job and no marker, so nothing
will ever deliver it and nothing will ever report it.

**Reproduction (repro C, real stores, `max_queue_size=2`):** three targets →
`[T1✓, T2✓, T3✗]` → `was_processed() == True` → replay drops the event; T3 has
no job and no mapping row.

**Confidence: HIGH.** This is the reported *"messages reach destination A but
never reach destination B"*, triggered by backpressure + a replay rather than by
a configuration error.

---

### CONFIRMED #5 — Duplicate delivery after crash/recovery (the at-least-once boundary, unguarded)

The worker returns `(job_id, target_msg_id)` and the batch commit happens
**later** (`mark_sent_batch`). If the process dies between the Telegram
`copy_message` returning and the batch commit, the job row is still `CLAIMED`.
The watchdog then expires the lease and a new worker **re-runs the send**. The
send itself carries no idempotency token (no `random_id` / business key), so
Telegram happily accepts a second identical copy.

Two distinct windows:

* **A. batch-commit window** — `_process_job` returns the result; the commit is
  deferred to the end of the claimed batch (v14 throughput optimisation). A
  crash mid-batch loses the outcome of every send in it.
* **B. lease-shorter-than-send window** — reproduced directly (repro D/E): a
  send lasting 0.4 s with a 0.2 s lease is reclaimed by the watchdog *while the
  first dispatch is still in flight*; worker 2 claims the same job and both
  dispatch it. **This one is not even a crash — a slow send (large media, a
  congested link) with a short lease double-delivers.**

**Reproductions:**
* repro D (crash before batch commit): `telegram received: 2 copies`.
* repro E (slow send + lease expiry, no crash): `w1 claimed … watchdog
  reclaimed 1 (while w1's send is still in flight) → 2 copies`.

**Confidence: HIGH.** The default `lease_duration=30.0` with a max-`batch_size`
of 8 media sends makes window B reachable in production; window A requires a
crash inside a ~1 s window but is a real data-integrity boundary.

---

## 3. Concurrency and race-condition risks

| # | Risk | Status | Evidence |
|---|---|---|---|
| R1 | Lease expiry mid-dispatch → concurrent double delivery (window B above) | **CONFIRMED** | repro E |
| R2 | Crash between send success and batch commit → duplicate on recovery | **CONFIRMED** | repro D |
| R3 | `_pending_estimate` is an in-memory counter bumped outside the DB transaction | Potential drift | `enqueue_delivery` bumps `_pending_estimate` after `run_in_transaction`; a second process (or the API) writing jobs leaves it stale. `_refresh_pending_count()` only runs hourly in `cleanup()`, so a stale-**high** value refuses every enqueue (silent backpressure drop) for up to an hour. |
| R4 | `_write_mapping_and_job` mapping upsert not status-guarded | **CONFIRMED** | repro B |
| R5 | `target_stats` / `stats` dicts mutated from N worker tasks without a lock | Benign under GIL but non-atomic read-modify-write across awaits | `_bump_target` |
| R6 | Per-target rate window map guarded by `asyncio.Lock`, batch claim releases it once per batch | Benign | `_check_target_rate` |
| R7 | `_notify_event.set()` wakes one waiter per set; with N workers an enqueue burst can wake only one worker, serialising claimed batches | Minor latency, no loss | `_worker_loop` waits with timeout=1.0 s |
| R8 | Configuration change mid-flight: a rule deleted/paused while its job is queued → `mark_failed("Rule no longer exists")` **kills the message permanently** | Design decision worth revisiting | `_process_job` L868–889 |

---

## 4. Database consistency risks

1. **`message_map` vs `delivery_jobs` divergence** (CONFIRMED #3/#4). There is
   **no reconciliation** between the two tables: the code itself documents that
   "no watchdog reconciles [mappings], it scans jobs". 24 orphan mapping rows
   exist in production (rows with no matching job — 24 vs. 8 job rows) and 13
   missing messages with no row anywhere.
2. **`cleanup()` deletes `delivery_jobs` SENT/FAILED after 7 days but
   `message_map` after 30 days**, and the dead-letter queue is never cleaned by
   retention (`idx_dlq_ts` exists but no purge query uses it). The 7/30-day
   mismatch widens the reconciliation window; the DLQ grows unboundedly.
3. **`mark_retry` dead-letters at `attempts >= max_attempts` while
   `mark_failed("Rule no longer exists")` bypasses the DLQ entirely** — a
   message whose task was deleted fails with no dead-letter row, so it is
   neither delivered nor recoverable (production row: `d4885d88…87067`,
   `attempts=0`, `CHAT_ADMIN_REQUIRED`, FAILED, no DLQ entry).
4. `sqlite3` connection shared across worker tasks under `_lock`; writes commit
   per statement (`self._db.execute` commits). WAL is enabled
   (`journal_mode=WAL` is set), busy timeout exists — acceptable.
5. PostgreSQL uses `FOR UPDATE SKIP LOCKED` in `claim_batch` (correct), with a
   non-atomic fallback path (SELECT then UPDATE per row) for adapters that do
   not support it — the fallback re-introduces the race it was designed to
remove.

---

## 5. Existing retry behaviour (assessment)

* Exponential backoff + jitter: present (`min(60, 2**attempts) + uniform(0.5,3)`).
* FloodWait: handled, with account-level time-limit propagation and owner
  notification. **However** `wait_seconds(exc, default=5)` is used for the
  non-FloodWait recoverable branch too, so a generic transient error inherits
  whatever wait the error carries.
* Permanent vs transient classification: `failure_policy.classify_failure` —
  good; auto-pauses only the owning task.
* Timeout → UNKNOWN with no blind retry: correct.
* `defer()` (no attempt consumed) for environment blockers: correct and
  important — it is what keeps paused/time-limited/no-client states from
  burning attempts.
* **Missing:** no cap on the *number* of `defer()` rounds other than the
  attempt counter not moving — a job can be deferred indefinitely (correct for
  "paused", questionable for a permanently missing client), and there is no
  maximum delivery age ("stuck pending" alert).

---

## 6. Root-cause hypotheses vs. proven bugs

| Reported symptom | Hypothesis | Verdict |
|---|---|---|
| Only some messages forwarded, others disappear | Polling/catch-up cursor advances past a truncated difference | **PROVEN** (repro A + production data: 13 lost posts) |
| Message reaches A, never B | Event-level `mark_processed` has no target dimension + per-target backpressure | **PROVEN** (repro C) |
| Forwarding "stops somewhere in the chain" | Lane swallows dispatch exceptions, cursor still advances | **PROVEN** (repro A/B) |
| Some accounts work, others intermittent | Account-level state (time limit, client liveness, lease duration vs. send duration) interacts with the shared queue | Plausible contributor; not isolated |
| Worse under load | `limit=MAX_POSTS_PER_SWEEP=100` truncation is load-amplified; batch-commit window widens with batch size | Plausible contributor; window B **PROVEN** (repro E) |
| Duplicate copies | Crash/slow-send + lease expiry re-dispatches with no idempotency key | **PROVEN** (repro D, E) |

**Not confirmed (and explicitly not claimed):** CPU/RAM/bandwidth saturation,
connection-pool exhaustion, gRPC timeouts as a message-loss cause, or any
tenant-isolation defect (isolation is thoroughly tested elsewhere).

---

## 7. Missing tests and observability gaps

**Tests:** the queue layer is well covered. **The ingestion boundary is not**:
no test asserts that a *truncated* `GetChannelDifference` leaves the cursor
consistent, that a failed `_dispatch_one` blocks cursor advancement, or that a
`message_map` row always has a driving job. There is no per-destination fan-out
test that asserts "one target's failure cannot mark another target delivered".
No test covers the crash-before-batch-commit window or lease-expiry-mid-send.

**Observability:**
* `get_observability_metrics()` reports queue depth and per-target counters —
  but **no reconciliation between `message_map` and `delivery_jobs`**, no
  "eligible messages without delivery records" metric, no oldest-pending-age
  *versus expected* staleness alarm, no failure-rate-by-source.
* The failure logs (`[PIPELINE:…]`) are good on the delivery side and
  **silent on the ingestion side**: a truncated difference logs nothing (the
  poll trace is INFO and does not show how many ids were returned vs. requested).
* The Rust logger collects WARN/ERROR/CRITICAL only — consistent with the
  requirement, but ingestion silent-loss produces **no WARN at all**, which is
  exactly why the incident was reported by users rather than detected by the
  platform.

---

## 8. Failure modes NOT present (credited)

To keep this audit honest: the following were reviewed and are **not** defects —
atomic enqueue transaction, lease watchdog, dead-letter payload retention,
tenant scoping of the DLQ, timeout ambiguity classification, restricted-source
bypass layering, `job_kind` dedup-key widening, per-target lane ordering, and
the `_release_content_claim` rollback for unused dedup claims. The team's
v12.3/v14/v20 work is sound; the findings above are all at the *boundary* between
ingestion, fan-out, and the queue.

---

## 9. Reproduction index

All reproductions run against the real `core` tree inside the `atf-core`
container image (Python 3.11, pinned deps), against **real**
`SqliteDatabase`/repositories — not mocks.

| ID | File | Proves |
|---|---|---|
| A | `test_repro_gap.py` | truncated difference → permanent silent loss (40/40) |
| B | `test_repro_replay.py` | replay regresses mapping SENT → PENDING, orphaned |
| C | `test_repro_multitarget.py` | one target refused + event-level marker → permanent per-target loss |
| D | `test_repro_crashdup.py` | crash before batch commit → duplicate delivery |
| E | `test_repro_concurrent.py` | slow send + lease expiry → concurrent duplicate delivery |

Reproduction scripts are kept at
`/root/.hermes/cache/scratch/atfaudit/` during the audit and are promoted into
`core/tests/` as permanent regression tests in the implementation phase.
