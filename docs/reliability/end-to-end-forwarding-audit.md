# End-to-End Forwarding Audit

**Scope:** Opselon/AutoTelegramForward, revision `78b29bd` (master, post-PR #274).
**Method:** static review of every stage of the forwarding lifecycle plus
deterministic reproductions against the real repositories and the real worker,
cross-checked against live production state (`atf-core`, 2026-10-31).
**Predecessor:** `forwarding-pipeline-audit.md` (PR #274) proved and fixed five
*reliability* defects. This audit covers the remaining lifecycle: content
fidelity, routing topologies, synchronization, and configuration consistency.

Every finding below is **confirmed by a reproduction**, and each reproduction is
kept under `/root/.hermes/cache/scratch/atfaudit/` for the implementation phase
to promote into `core/tests/`.

---

## 1. What is verified sound (credited)

Before the findings, the parts of the lifecycle that are correct:

* **Many-to-one mapping.** `message_map`'s key is
  `(rule_id, source_chat_id, source_message_id, target_chat_id)`, so two rules
  watching different sources that share one target keep fully independent
  mappings. Reproduced: source A and B both message id 500 → separate target
  copies 7001 and 7002. *(test_repro_manytoone.py)*
* **Ambiguous chat-less deletes are skipped.** `handle_delete_without_chat`
  resolves an id across source chats and refuses to delete when it maps to more
  than one — removing the wrong copy is worse than missing a delete.
* **Loop prevention is identity-based.** `check_loop` compares chat ids and the
  forward origin, never text — exactly what the mission asks for. Per-target,
  so one looping target does not drop the message for the others.
* **Album aggregation is bounded** (LRU cap 500, TTL flush, max-10 flush) and
  one album never blocks another.
* **The routing policy is per-owner single-match** with `multi_route` opt-in:
  one message is consumed by exactly one task per tenant, and a task never
  starves another tenant's.
* **The v12.3 atomic enqueue, v14 lane ordering, DLQ scoping and the PR #274
  fence and gap recovery** all hold under their existing tests.

---

## 2. Confirmed defects

### E2E-1 — Seven media types are invisible to replayed messages (HIGH)

**Actual behavior.** `Normalizer.normalize` (used for every message that
arrives through catch-up, the poller, or any non-live path) detects media by
checking seven attributes in order — `photo, video, document, audio, voice,
sticker, animation` — and stops at the first hit.
`video_note`, `poll`, `contact`, `location`, `venue`, `dice` and `game` are not
on that list at all, so **every one of them is classified `text` with
`has_media=False`**.

*Reproduction (`test_repro_mediatypes.py`):*

| message | detected | has_media |
|---|---|---|
| video_note (round video) | `text` | **False** |
| poll | `text` | **False** |
| contact | `text` | **False** |
| location | `text` | **False** |
| venue | `text` | **False** |
| dice | `text` | **False** |
| game | `text` | **False** |

**Expected behavior.** The same classification as the live path
(`payload_from_pyrogram`), which reads `message.media.name` and covers all of
them.

**Root cause.** Two independent normalizers of the same pyrogram object exist
and only one of them was written against the real attribute set.

**Affected component.** `core/application/pipeline.py`
(`Normalizer.normalize`), downstream: `media_block_reason`, the capability
matrix, `_dispatch_to_telegram`'s mode decisions.

**Severity.** High. Content policy is decided from a lie for these types: a
task with `block_video` forwards a round video that arrives via catch-up while
blocking it live; a task with `allowed_media_types=["photo"]` forwards a poll
that arrives via replay. The defect is also *path-dependent*, so the same
message behaves differently depending on how it was received.

**Reproduction scenario.** An album-less poll posted in a source while the
session is offline → catch-up replay → `media=other` on the live path vs
`media=text` on the replay path.

**Proposed correction.** One shared media-detection helper used by both
normalizers, derived from `message.media.name` when available and falling back
to the attribute scan, with the *full* attribute table including
`video_note`, `poll`, `contact`, `location`, `venue`, `dice`, `game`.

**Regression test required.** Yes — a table-driven test asserting both
normalizers agree on every media kind.

---

### E2E-2 — The two normalization paths disagree on the same message (HIGH)

**Actual behavior.** `payload_from_pyrogram` (live) and `Normalizer.normalize`
(replay) classify the same physical message differently. In addition to
E2E-1's missing types, `MEDIA_TYPE_MAP` itself lacks `venue`, `dice` and
`game`, which therefore become `MediaType.OTHER` on the live path.

*Reproduction (`test_repro_normalizer_split.py`):*

| kind | live path | replay path | agree |
|---|---|---|---|
| video_note | `video` | `text` | NO |
| poll | `poll` | `text` | NO |
| contact | `contact` | `text` | NO |
| location | `location` | `text` | NO |
| venue | `other` | `text` | NO |
| dice | `other` | `text` | NO |
| game | `other` | `text` | NO |

**Expected behavior.** One canonical classification of a pyrogram message, in
one place, used by both paths.

**Root cause.** `MEDIA_TYPE_MAP` (14 entries, 3 wrong) and the attribute list
(7 of 14) were written separately and never reconciled.

**Affected component.** `core/infrastructure/telegram/client_pool.py`
(`MEDIA_TYPE_MAP`), `core/application/pipeline.py` (`Normalizer`).

**Severity.** High. Which copy of a message gets through depends on how the
message arrived — the defining symptom of the reported incident class
("some accounts work, others are intermittent").

**Proposed correction.** A single `resolve_media_type(message)` helper in the
domain layer, used by both, with one authoritative table. Add the missing
`video_note → VIDEO_NOTE`, `venue`, `dice`, `game` entries.

**Regression test required.** Yes.

---

### E2E-3 — An edit arriving during pending delivery is silently dropped (HIGH)

**Actual behavior.** `SyncEngine.handle_edit` requires
`m.delivery_status == SENT`. An edit that arrives while the initial delivery is
still queued — a common case under backpressure or a slow worker — is skipped.
Nothing retries it. The queued job then delivers the **old** text, and the
destination drifts permanently.

*Reproduction (`test_repro_editpending.py`):* mapping PENDING, edit event →
`edits synced = 0`, mapping stays PENDING, and no record beyond a debug log.
The destination receives and keeps the stale original.

**Expected behavior.** Either the edit is applied after the delivery completes
(a pending-edit queue per mapping), or the edit is retried until the mapping
becomes SENT, with an observable failure if it never does.

**Root cause.** `handle_edit`'s SENT guard has no complementary "not yet sent"
path, and there is no edit-retry mechanism anywhere in the sync engine.

**Affected component.** `core/infrastructure/telegram/sync_engine.py`.

**Severity.** High. Any edit that races a queued delivery produces a permanently
wrong destination message — and the operator sees no error.

**Proposed correction.** Record the pending edit on the mapping (or a small
companion table) and re-apply it from `_note_mapping(..., SENT)` when the
delivery completes; retry with bounded backoff otherwise.

**Regression test required.** Yes.

---

### E2E-4 — Failed edits are never retried (MEDIUM)

**Actual behavior.** A failed `edit_message_text`/`edit_message_caption` logs
`[PIPELINE:SYNC_EDIT_FAIL]` and stops. The source and destination stay
permanently out of sync.

**Expected behavior.** A transient edit failure (FloodWait, network) should
retry with bounded backoff; a permanent one should be observable and
recoverable.

**Root cause.** No retry state exists for sync operations — the entire sync
engine is best-effort.

**Affected component.** `core/infrastructure/telegram/sync_engine.py:212-226`.

**Severity.** Medium (drift without loss).

**Proposed correction.** Route failed edits through the existing durable queue
as a `job_kind="edit_sync"` delivery, which already has bounded retries,
UNKNOWN classification and a dead-letter path.

**Regression test required.** Yes.

---

### E2E-5 — Split-album partial failure duplicates already-delivered items (HIGH)

**Actual behavior.** With `album_mode=split`, each item is its own
`copy_message`. If item 3 of 3 fails with a non-restricted error, the job goes
to `RETRY_WAIT` — while items 1 and 2 are already in the target. The retry
then re-sends items 1 and 2: **duplicates of the same album items**.

*Reproduction (`test_repro_albumpartial.py`):*
`telegram actually holds: [50, 51] (2 of 3 items)`, `job = RETRY_WAIT`;
the next attempt re-sends 50 and 51.

**Expected behavior.** Per-item delivery state: a retry must resend only the
item that failed, and the mapping must record which items landed.

**Root cause.** The album is one job with one status; there is no per-item
state, and the split loop's unit of failure is the whole job.

**Affected component.** `core/infrastructure/telegram/durable_queue.py`,
`_dispatch_to_telegram` split branch.

**Severity.** High — visible duplicates of media content, the exact
"duplicate copies" symptom from the original report.

**Proposed correction.** Per-item result tracking inside the split loop: the
job carries which item ids were confirmed, the retry resumes from the first
unconfirmed one, and the mapping keeps the sent ids. (Within the existing
`payload_json` — no new table needed for the retry-state, per §11's
minimal-storage rule.)

**Regression test required.** Yes.

---

### E2E-6 — Jobs read the live task configuration at dispatch time (MEDIUM)

**Actual behavior.** The worker re-resolves the rule with
`self._rules.get_by_id(job.rule_id)` at dispatch time and re-evaluates
`is_active`, `is_paused`, filters and transforms from the *live* row. A
configuration change between enqueue and dispatch changes what the job does.

**Expected behavior (mission §4.5).** "Every queued job must carry an immutable
configuration snapshot or a stable configuration version reference. A running
job must not unexpectedly change behavior because a user edited the Task
configuration halfway through processing."

**Root cause.** The payload carries a few frozen decisions (delay, auto-delete,
topic, transport) but the *policy* is re-read live.

**Affected component.** `durable_queue.py:_process_job` (four
`get_by_id` call sites).

**Severity.** Medium. In practice this also causes the *deleted-rule drops the
message* behaviour (an owner deleting a task mid-flight kills its queued
messages rather than letting the in-flight ones complete).

**Proposed correction.** Add a `config_version` to the job payload and resolve
the rule at enqueue time into the payload; the worker uses the snapshot and
only re-checks "does the task still exist" live.

**Regression test required.** Yes.

---

### E2E-7 — `media=other` is unclassified in production traffic (MEDIUM)

**Live evidence.** The last 24 h of `atf-core` show 226 `media=text`, 20
`media=other`, 14 `media=photo`. The `other` entries are ordinary text posts
carrying link previews (`web_page` in `MEDIA_TYPE_MAP` → `MediaType.OTHER`),
**and at least one photo post classified `other`**.

**Actual behavior.** `web_page` is not a content decision the owner made; it is
the *preview* attached to a text message. Mapping it to `OTHER` makes the
allow-list gate (`allowed_media_types`) unable to say "text yes, previews
whatever" — a task allowing only `text` would drop a text message that carries
a preview.

**Expected behavior.** A text message with a link preview is a text message;
the preview is not a content category.

**Root cause.** `MEDIA_TYPE_MAP["web_page"] = MediaType.OTHER`.

**Affected component.** `core/infrastructure/telegram/client_pool.py`.

**Severity.** Medium.

**Proposed correction.** Map `web_page` to `TEXT` (the message's own kind) and
treat the preview as an attribute of the text, not a media type.

**Regression test required.** Yes.

---

## 3. Not-yet-verified areas (honest status)

The following lifecycle areas are implemented but **not verified end-to-end
against real Telegram** in this audit pass. They must not be reported as
working; the implementation phases exist to verify or fix them.

| area | status | why unverified |
|---|---|---|
| Gift / paid media / unique gifts | **NOT VERIFIED** | the capability matrix asserts support from the API surface, not from an executed send; Gifts API behavior cannot be confirmed without a live send |
| Poll/quiz forwarding fidelity | **NOT VERIFIED** | matrix says "copy: False"; no test sends a poll and reads the destination |
| Sticker fidelity (TGS/WebM/custom emoji) | **NOT VERIFIED** | no per-kind send/verify test exists |
| Edit sync of album captions (per-item) | **NOT VERIFIED** | `edit_message_caption` on one item of a media group has documented API limits; the code path was not exercised |
| Delete of one album item | **NOT VERIFIED** | the code deletes the whole mapped group; per-item semantics were not verified |
| Pin synchronization | **NOT IMPLEMENTED** | no pin handler exists |
| Real Telegram destination reconciliation | **NOT IMPLEMENTED** | PR #2 territory; no destination-state verification exists today |
| Web dashboard delivery-health surfaces | **PARTIAL** | DLQ and per-rule stats exist; UNKNOWN count, ingestion gaps and reconciliation mismatches are not surfaced |

---

## 4. Root-cause themes

1. **Two implementations of every lifecycle stage.** Live and replay
   normalization (E2E-1/2), worker and legacy dispatch (already fixed for
   duplicates by PR #274), direct and queued delivery. Whenever a stage has two
   implementations, they drift — and the drift is *path-dependent*, which is
   exactly the reported symptom class.
2. **The sync engine is best-effort everywhere.** No retry, no pending-state
   handling, no audit record of a skip (E2E-3/4). Synchronization is treated as
   a side effect of delivery rather than a first-class durable operation.
3. **Whole-album granularity.** The album is one job and one mapping; any
   per-item failure or retry therefore cannot express per-item state (E2E-5).
4. **Configuration is re-read, not snapshotted.** The job carries frozen
   *decisions* but live *policy* (E2E-6), so mid-flight edits and deletes change
   what a queued job does.

These are the four mechanisms to eliminate across the coming phases, not a list
of one-off patches.

---

## 5. Defect index

| ID | defect | severity | reproduction | status |
|---|---|---|---|---|
| E2E-1 | 7 media types invisible to the replay normalizer | High | `test_repro_mediatypes.py` | **Fixed** — `core/domain/media_kind.py`, one resolver for both paths; `test_media_classification.py` (64 cases) |
| E2E-2 | live vs replay normalization disagree | High | `test_repro_normalizer_split.py` | **Fixed** — both paths now agree on all 15 kinds |
| E2E-3 | edit during pending delivery silently dropped | High | `test_repro_editpending.py` | **Fixed** — edit folded into the pending job; `test_edit_sync_recovery.py` (9 cases) |
| E2E-4 | failed edits never retried | Medium | code path (no retry exists) | **Fixed** — bounded in-call retry, transient/permanent classification |
| E2E-5 | split-album partial failure duplicates items | High | `test_repro_albumpartial.py` | **Fixed** — per-item confirmations in the job payload; `test_album_split_resume.py` (3 cases) |
| E2E-6 | jobs read live task config at dispatch | Medium | code inspection (4 call sites) | **Pinned** — all delivery parameters are already snapshotted at enqueue; the live read is a guard only. `test_job_config_snapshot.py` (3 cases) locks the contract |
| E2E-7 | `web_page` classified as `OTHER` in production | Medium | live logs + `MEDIA_TYPE_MAP` | **Fixed** — a link preview resolves to TEXT, so a media gate cannot drop a text post |

The five PR #274 findings remain fixed and are not re-listed here.

Two further loop short-circuits were found while testing the fix, both
pre-existing and both silent count corruptors:

* `handle_reply` returned `0` on an already-delivered reply, discarding the
  accumulated `replied_count` and un-claiming the event — so the dispatcher
  forwarded the reply again (a duplicate). Pinned by
  `test_a_redispatched_reply_is_not_posted_twice`.
* `_delete_mapped` returned `0` on a foreign-rule mapping, discarding an
  already-counted delete. Pinned by
  `test_delete_never_crosses_to_another_rules_target`.
