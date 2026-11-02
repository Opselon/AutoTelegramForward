# Agent C — Routing, Pipeline & Event Lifecycle

Stage 4.0 discovery-only audit. Scope: `EVT-*`, `DUP-*`, `FEN-*`, `KW-*`, `BRD-*` (pipeline side), albums, and the canonical processing-order map.
Baseline: master `35ee530cc80cd17d1ac7d702c4e5f5ea8024889a`, branch `audit/complete-business-logic-inventory`.
All claims are `file:line` citations into that baseline. Nothing outside this file was written.

---

## 1. Event lifecycle (EVT-*)

### 1.1 Handler registration and the `_handler` branch order (verified)

Handlers are attached per live session in `MessageDispatcher.bind_session`
(`core/infrastructure/telegram/dispatcher.py:188-195`):

| Pyrogram event | Handler | Trigger string |
|---|---|---|
| `message` | `_make_handler("NEW_MESSAGE", session_id)` (`dispatcher.py:192`) | `NEW_MESSAGE` |
| `edited_message` | `_make_handler("EDITED_MESSAGE", session_id)` (`dispatcher.py:193`) | `EDITED_MESSAGE` |
| `deleted_messages` | `_make_deleted_handler()` (`dispatcher.py:194`, def at `dispatcher.py:237`) | — (no pipeline call) |

One `MessageHandler` registration covers private chats, groups, supergroups **and** channel posts (comment at `dispatcher.py:189-191`); there is no separate channel-post handler. The closure is cached per session at `dispatcher.py:205` (`self._message_handlers[session_id] = handlers[0][0]`) so the catch-up poller can replay through the identical path (`dispatcher.py:223-229`, `message_handler()`).

**`_handler` branch order** (`dispatcher.py:314-404`, entry `_make_handler` at `dispatcher.py:313`). Each branch is checked in this fixed sequence and every early branch `return`s:

| # | Branch | Lines | On match | On miss |
|---|---|---|---|---|
| 0 | Normalize via `payload_from_pyrogram(message)`; failure → log and return | `dispatcher.py:316-319` | — | — |
| 1 | **AI PV auto-reply**, `trigger == "NEW_MESSAGE"` AND `chat.type` endswith `private` | `dispatcher.py:332-359` | `self._autoreply.handle_incoming(inbound)` (v27 engine, enqueue-only) `dispatcher.py:345`; else legacy `self._pv_responder.handle_message` which **returns** when it replied (`dispatcher.py:353-357`) | falls through — **the autoreply branch does NOT return**, so a PV message also continues to the forwarder |
| 2 | **EDITED_MESSAGE → `_route_edit`** | `dispatcher.py:362-376` | `await self._route_edit(payload)`; if True → `return` (edit consumed by sync engine) | falls through as a **first delivery** of the edit |
| 3 | **NEW_MESSAGE with `reply_to_message_id` → `_route_reply`** | `dispatcher.py:383-388` | `await self._route_reply(payload)`; if True → `return` (reply mirrored) | falls through to normal forwarding |
| 4 | **Album: `payload.media_group_id` AND `trigger == "NEW_MESSAGE"`** | `dispatcher.py:391-394` | `await self._album_aggregator.ingest(sid, payload)` then `return` (single buffered event) | falls through |
| 5 | **Pipeline** | `dispatcher.py:396-402` | `await self._durable_pipeline.process(payload, trigger=trigger)`, or `self._use_case.process_message(...)` when no durable pipeline | — |

**Correction to the brief's §1 outline (item 1):** the outline says "AI PV auto-reply … (NEW_MESSAGE + private chat) — dispatcher.py:325-348". The branch is real and verified, but its position is **after** the log line at `dispatcher.py:324-329`, and — importantly — it is **not** a terminal branch for the v27 engine: only the legacy `_pv_responder` path can `return` (`dispatcher.py:356-357`). The numbering `325-348` is approximately right; the semantic nuance (that the modern engine never terminates the event) is not captured in the brief.

`_route_edit` (`dispatcher.py:273-288`): probes `self._sync_engine.has_mapping(chat_id, message_id)`; mapped → `sync_engine.handle_edit(payload, payload.effective_text)` and returns True; unmapped → returns False so the edit is forwarded once. `has_mapping` **fails closed** — a mapping-store error returns True (claims the edit) rather than letting the pipeline re-post it (`sync_engine.py:94-117`).

`_route_reply` (`dispatcher.py:290-311`): claims only when `payload.reply_to_message_id` is set and `sync_engine.handle_reply` returns > 0.

### 1.2 Per-event-type full paths

**EVT-01 New message (channel post / group / PV)**
`bind_session` → `_make_handler("NEW_MESSAGE")` (`dispatcher.py:192`) → `_handler` (`dispatcher.py:314`) → `payload_from_pyrogram` (`dispatcher.py:316`) → [PV-only autoreply side effect, non-terminal] → [reply claim] → [album ingest] → `DurableMessagePipeline.process(payload, "NEW_MESSAGE")` (`dispatcher.py:398`) → `process()` at `core/application/pipeline.py:633` → INGEST (`pipeline.py:637-644`) → NORMALIZE (`pipeline.py:646-679`, `Normalizer.normalize` at `pipeline.py:186`) → ingestion-gap resolution (`pipeline.py:659-660`, `_resolve_ingestion_gap` at `pipeline.py:607`) → service-message drop (`pipeline.py:681-685`) → rule routing (`pipeline.py:688-690`) → per-rule `_process_for_rule` (`pipeline.py:731-733`). Status: `FULLY_IMPLEMENTED`.

**EVT-02 Edited message**
`_make_handler("EDITED_MESSAGE")` (`dispatcher.py:193`) → `_handler` → branch 2 (`dispatcher.py:362`) → `_route_edit` (`dispatcher.py:273`) → `SyncEngine.has_mapping` (`sync_engine.py:94`) → mapped: `SyncEngine.handle_edit` (`sync_engine.py:119`) → per mapping: `SENT+target_message_id` → `_sync_sent_edit` (`sync_engine.py:194`); else `_fold_edit_into_pending_job` (`sync_engine.py:154`, via `jobs_repo.fold_edit`). Unmapped edit → falls through to `durable_pipeline.process(payload, "EDITED_MESSAGE")`, where the per-rule gates `ignore_edits` (`pipeline.py:811-818`) and the edit-time window (`pipeline.py:824-835`, `AdvancedSettingsEngine.is_edit_within_window` at `services.py:1538-1556`) apply. Status: `FULLY_IMPLEMENTED`.

**EVT-03 Edited caption**
No dedicated branch. A caption edit arrives as `EDITED_MESSAGE` with `payload.has_media` true, and `SyncEngine._sync_sent_edit` picks `client.edit_message_caption` vs `client.edit_message_text` from `payload.has_media` (`sync_engine.py:271-288`). In the pipeline the caption is part of `payload.effective_text` (Normalizer carries `caption` at `pipeline.py:229`). Status: `FULLY_IMPLEMENTED` (no separate event type exists).

**EVT-04 Deleted message**
`_make_deleted_handler` (`dispatcher.py:237-271`). Buckets ids by chat when `updateDeleteChannelMessages` names the channel; unresolved ids (from `updateDeleteMessages`, private/basic-group) go to `handle_delete_without_chat` (`dispatcher.py:265-268`). → `SyncEngine.handle_delete` (`sync_engine.py:349-359`) → `_delete_mapped` (`sync_engine.py:390-453`): per mapping, gate `rule.is_active` + `rule.sync_deletes` (`sync_engine.py:402-407`), `client.delete_messages` under `_throttled` (`sync_engine.py:423-429`); `MessageIdInvalid` counts as already-deleted (`sync_engine.py:441-443`). `handle_delete_without_chat` (`sync_engine.py:361-388`) resolves the id through `list_by_source_message_id` and **skips ambiguous ids** mapping to >1 source chat (`sync_engine.py:378-386`). **Deleted messages never enter the pipeline** — there is no `DELETED_MESSAGE` trigger and no `_process_for_rule` path. Status: `FULLY_IMPLEMENTED`.

**EVT-05 Reply**
`_handler` branch 3 (`dispatcher.py:383`) → `_route_reply` (`dispatcher.py:290`) → `SyncEngine.handle_reply` (`sync_engine.py:455-546`): resolves mappings for the *replied-to* id (`sync_engine.py:476`), requires non-empty text (`sync_engine.py:480-482`), idempotency via the reply's own message_map rows (`sync_engine.py:486-491`), gates on `m.delivery_status == SENT`, `rule.is_active`, `rule.sync_replies` (`sync_engine.py:495-502`), posts `client.send_message(reply_to_message_id=m.target_message_id)` (`sync_engine.py:523-530`) and records the delivered reply via `_record_reply` → `message_map.add_or_update` (`sync_engine.py:548-565`). A non-zero count claims the event (`dispatcher.py:385-386`); zero (no mapping / switch off / no text) falls through to plain forwarding. Status: `FULLY_IMPLEMENTED`.

**EVT-06 Album / media group**
`_handler` branch 4 (`dispatcher.py:391-394`) → `AlbumAggregator.ingest(sid, payload)` keyed on `(session_id, chat_id, media_group_id)` (`album_aggregator.py:72`). First piece opens the group and starts a TTL timer (`album_aggregator.py:99-103`); later pieces append (`album_aggregator.py:79-88`) with duplicate-id guard (`album_aggregator.py:37-38`); flush at 10 items (Telegram's album cap, `album_aggregator.py:83-88`), at TTL expiry (`album_aggregator.py:105-110`), on LRU eviction at 500 active albums (`album_aggregator.py:91-98`), or at `stop()` (`album_aggregator.py:134-144`). Flush calls `_flush_album` (`dispatcher.py:406-414`): sets `first_msg.album_messages = list(all_msgs)` and processes the first payload with trigger `NEW_MESSAGE`. Note: constructor TTL is 2.0s (`dispatcher.py:96`); the `ForwardRule.album_aggregation_window_seconds` property exists (`entities.py:410`, default 0.8) but **the dispatcher hard-codes 2.0 and does not read the rule's setting** — see parity finding P-4 and Unknowns. Status: `FULLY_IMPLEMENTED` with a stored-but-unused setting (`album_aggregation_window_seconds`).

**EVT-07 Service event**
Detected at `Normalizer.normalize` (`pipeline.py:204`, `is_service`) from the raw `service` attribute, and dropped before rule matching at `pipeline.py:681-685` (reason `service_message_dropped`). Service messages therefore never reach dedup/filter/transform. Status: `FULLY_IMPLEMENTED`.

**EVT-08 Historical / backfill**
Two paths. (a) **Channel polling**: `ChannelPollingService` (`core/infrastructure/telegram/channel_polling.py:248`) sweeps source channels, computes missing ids (`_missing_ids`, `channel_polling.py:1047-1061`), records them as ingestion gaps, and replays each found post through `_replay_one` (`channel_polling.py:1082-1098`) → `_OrderedReplayLane` (`channel_polling.py:165`) → `_dispatch_one` (`channel_polling.py:1100`) → `dispatcher.message_handler(session_id)` (`channel_polling.py:1104-1109`), i.e. **the exact same `_handler` closure as live push**. (b) **Gap recovery**: `_recover_gaps`/`_recover_one_gap` (`channel_polling.py:348-389`). The loop is closed by the pipeline: `process()` resolves the gap row in `_resolve_ingestion_gap` (`pipeline.py:607-631`) right after NORMALIZE (`pipeline.py:659-660`) — deliberately *before* any filtering so a gap row can never survive an id the pipeline received. Status: `FULLY_IMPLEMENTED`.

**EVT-09 Polling-recovered**
Same as EVT-08: the recovered event's dedup safety is `skip_history` + `processed_messages.was_processed` at `pipeline.py:837-846` (applies only when `not payload.is_edit`), plus the content-fingerprint claim at `pipeline.py:861-886`. Replay ordering is per-channel ascending message id, one at a time, by the ordered lane (`channel_polling.py:1085-1093`, `_drain` at `channel_polling.py:202`). Status: `FULLY_IMPLEMENTED`.

**EVT-10 Trigger-event filtering (rule-level)**
After routing, `valid_rules` keeps only rules whose `trigger_events` contains the current trigger (`pipeline.py:690`); default is `[NEW_MESSAGE]` (`entities.py:421`). A rule that does not list `EDITED_MESSAGE` therefore never sees edits. Status: `FULLY_IMPLEMENTED`.

### 1.3 Pipeline stage model (verified from `PipelineStage`, `pipeline.py:60-72`)

Nine declared stages: `INGEST`, `NORMALIZE`, `DEDUPLICATE`, `FILTER`, `FORWARD_INTERVAL`, `TRANSFORM`, `ENQUEUE`, `DISPATCH`, `CONFIRM`, `MESSAGEMAP`. The module docstring (`pipeline.py:3-4`) calls it a 9-stage flow; in practice DISPATCH/CONFIRM/MESSAGEMAP are owned by the queue worker or the Go Worker, not by `process()`. `PipelineContext.is_failed` treats FILTER, DEDUPLICATE and FORWARD_INTERVAL "failures" as intentional skips, not errors (`pipeline.py:117-129`).

---

## 2. Canonical pipeline stage order (the critical deliverable)

Verified end-to-end from `core/application/pipeline.py`. **Two corrections to the brief's §12/§2 outline are marked ⚠ below.**

### 2.1 Event level — `DurableMessagePipeline.process()` (`pipeline.py:633-746`)

| # | Step | Lines | Level | On failure/skip |
|---|---|---|---|---|
| E1 | INGEST: null check | `pipeline.py:639-643` | event | `reason="null_message"`, return |
| E2 | NORMALIZE: `Normalizer.normalize` | `pipeline.py:648-651` | event | exception → `normalize_error`, parser diagnostic (`pipeline.py:675-678`), return |
| E3 | Ingestion-gap resolution (`_resolve_ingestion_gap`) | `pipeline.py:659-660` | event | best-effort, never fails the message (`pipeline.py:620-631`) |
| E4 | Service-message drop | `pipeline.py:681-685` | event | `service_message_dropped`, return (before routing) |
| E5 | Rule routing: `list_active_by_source` + `RoutingPolicy.matching_rules` | `pipeline.py:688-689` | event | no match → `no_matching_rules` / `rule_paused` if every active rule is paused (`pipeline.py:692-724`) |
| E6 | Trigger-event filter (`trigger_events`) | `pipeline.py:690` | event | drops the rule from `valid_rules` |
| E7 | Per rule: `_process_for_rule`, then `finally: _release_content_claim` | `pipeline.py:731-738` | rule | claim release even on exception |

`matching_rules` (`core/domain/services.py:622-655`) sorts by `(-priority, execution_order, created_at)` and applies a **single-match policy per owner**: one message is consumed by exactly one task per tenant unless that task sets `multi_route` (`services.py:650-653`). Two tenants watching the same channel both forward it.

### 2.2 Per rule — `_process_for_rule()` (`pipeline.py:778-1399`)

| # | Stage gate | Lines | On skip/drop | Notes |
|---|---|---|---|---|
| R0 | `materialize(rule, bot_tokens)` — attached-bot defaults | `pipeline.py:786` | — | works on a copy |
| R1 | Rule paused / auto-paused (`is_paused` + `is_currently_paused()`) | `pipeline.py:788-797` | `rule_paused`, return | ⚠ **The brief's outline omits the `materialize` step R0 that precedes it.** |
| R2 | `since_ts` too-old (`payload.date < since`) | `pipeline.py:801-809` | DEDUPLICATE `too_old`, return | event-level window |
| R3 | `ignore_edits` (edits only) | `pipeline.py:811-818` | DEDUPLICATE `edit_ignored`, return | |
| R4 | Edit-time window `is_edit_within_window` | `pipeline.py:824-835` | DEDUPLICATE `edit_outside_time_window`, return | `edit_time_limit_seconds` 0 = no window (`services.py:1552`) |
| R5 | `skip_history` + `was_processed` (**non-edit only**) | `pipeline.py:837-846` | DEDUPLICATE `duplicate_event`, return | `processed_messages` store; `INSERT OR IGNORE` at `sqlite_repositories.py:1322-1329` |
| R6 | Content dedup: `duplicate_filters` → `_content_fingerprint` → `claim_content_hash` | `pipeline.py:861-886` | DEDUPLICATE `duplicate_content`, return | claim stored in `ctx.content_claims` (`pipeline.py:874`); store hiccup → **fail open** (`pipeline.py:877-878`) |
| R7 | DEDUPLICATE success record | `pipeline.py:888` | — | |
| R8 | Attached `FilterRule` fetch + **owner check** | `pipeline.py:892-916` | filter owned by another tenant → ignored (not dropped), `filter.denied` audit | `owner not in (0, task_owner)` (`pipeline.py:905`) |
| R9 | **`FilterEngine.evaluate(payload, filter_rule)`** | `pipeline.py:918` | `DROP` → FILTER drop, return (`pipeline.py:921-928`) | the FLT engine, Agent A's scope |
| R10 | Session time-limited **defer** (not drop) | `pipeline.py:935-949` | records `session_time_limited_deferred`, continues | job is enqueued and the worker defers |
| R11 | `media_block_reason` / `link_block_reason` | `pipeline.py:955-966` | FILTER drop with reason, return | shared decision in `core/domain/media_filter.py:77-98` |
| R12 | **Content policy chain (task + source-scoped) evaluate** | `pipeline.py:974-1016` | any deny → FILTER drop, `CONTENT_POLICY` audit, return | `rule.policy_chain(source_chat_id=...)` (`pipeline.py:974-977`); `media_type` passed in (`pipeline.py:985`) |
| R13 | FILTER success record | `pipeline.py:1018-1023` | — | |
| R14 | **`forward_every_n` slot gate (FORWARD_INTERVAL)** | `pipeline.py:1036-1084` | not slot owner → marks processed, `every_n_skip`, return | see §4 |
| R15 | TRANSFORM: `_apply_transforms` | `pipeline.py:1088-1089` | `None` → transform drop/defer, return (`pipeline.py:1090-1097`) | 1024/4096 length clamp at `pipeline.py:1099-1101` |
| R16 | ENQUEUE: resolve targets (`all_targets`) | `pipeline.py:1119-1126` | none → `no_targets`, return | |
| R17 | Branding hop **decision** (`branding_decision`) | `pipeline.py:1137-1160` | C-is-source / no-remaining-target → hop disabled, direct delivery (`pipeline.py:1147-1157`) | builds `delivery_targets` excluding C |
| R18 | Per-target loop (see §2.3) | `pipeline.py:1163-1296` | per-target `continue` on loop-skip / target filter reject | |
| R19 | **Keyword reply enqueue** (after the primary enqueue) | `pipeline.py:1298-1346` | failure → warning only, forward unaffected (`pipeline.py:1340-1346`) | see §5 |
| R20 | ENQUEUE stage record | `pipeline.py:1348-1353` | — | reason `enqueued` / `backpressure_or_duplicate` |
| R21 | `mark_processed` — **gated on the whole fan-out** | `pipeline.py:1366-1393` | partial fan-out → marker NOT written, event stays replayable | `enqueued_any and not enqueue_failures` (`pipeline.py:1368`) |
| R22 | Content claim **earned** (pop, keep the row) | `pipeline.py:1398-1399` | — | |

### 2.3 Per target — inside the loop (`pipeline.py:1163-1296`)

| # | Step | Lines | On skip |
|---|---|---|---|
| T1 | Anti-loop check `RoutingPolicy.is_loop` | `pipeline.py:1168-1182` | `continue` (skip this target only; other targets still receive) |
| T2 | Target-scoped policy **evaluate** — only `source` startswith `target:` | `pipeline.py:1197-1205` | reject → `target_filtered:<target>`, `continue` (`pipeline.py:1206-1219`) |
| T3 | Target-scoped policy **transform** (`chain[1:]`, active only) | `pipeline.py:1220-1229` | rewrites `target_text` on this target's copy |
| T4 | Branding hop per distinct text (`hop_plans` dedup) | `pipeline.py:1243-1254` | intent mode under worker ownership (`pipeline.py:1245-1249`); performed hop otherwise (`pipeline.py:1250-1254`) |
| T5 | `enqueue_delivery` | `pipeline.py:1256-1261` | False → `enqueue_failures.append(target)` + `QUEUE_ENQUEUE_FAILED` audit (`pipeline.py:1280-1293`) |
| T6 | Job bookkeeping (`ctx.target_jobs`, metrics, `forward.enqueued` audit) | `pipeline.py:1262-1279` | — |

### 2.4 Verified TRANSFORM order inside `_apply_transforms` (`pipeline.py:1401-1579`)

This is Agent B/D territory; it is reproduced here only to fix the boundary of the C-owned stages. Verified order: CleanerRule.clean (`pipeline.py:1410-1419`) → LinkManagementEngine.apply_to_payload or legacy `replace_links` (`pipeline.py:1437-1457`) → `FilterEngine.remove_emojis` (`pipeline.py:1460-1461`) → `_clamp_entities` (`pipeline.py:1466`) → empty-after-clean drop (`pipeline.py:1468-1486`) → `ContentPolicy.transform` (`pipeline.py:1496-1514`) → AI transform (`pipeline.py:1520-1536`) → `FilterEngine.replace_text` (`pipeline.py:1542-1543`) → header/footer (`pipeline.py:1546-1549`) → `AdvancedSettingsEngine.apply_content_transforms` (`pipeline.py:1554-1565`) → final `_clamp_entities` (`pipeline.py:1577`). This matches the coordinator's verified order exactly.

### 2.5 Corrections to the brief's illustrative outline

1. ⚠ **The `materialize(rule, self._bot_tokens)` call at `pipeline.py:786` is missing from the brief's sequence.** It precedes the paused check and is behaviorally significant: every later gate reads the merged bot-defaults view, not the task row.
2. ⚠ **The target-scoped policy evaluate at `pipeline.py:1197-1205` filters `source.startswith("target:")` before evaluating** — the brief wrote "target policy evaluate+transform (1197-1214)" as if the whole chain re-ran. Only `target:`-prefixed policies are *decided* here; `chain[1:]` is then transformed. This matches the semantic documented at `pipeline.py:1186-1196`.
3. The enqueue ordering is otherwise **confirmed as stated** in the coordinator vocabulary: every gate from R1 to R12 precedes FORWARD_INTERVAL (R14), which precedes TRANSFORM (R15), which precedes the per-target loop.

---

## 3. Deduplication (DUP-*)

### DUP-1 Event-level replay protection (`skip_history` + `processed_messages`)
- Gate: `pipeline.py:837-846` — only when `not payload.is_edit` and `rule.skip_history` (default True, `entities.py:415`) and `self._processed is not None`.
- Read: `IProcessedMessageRepository.was_processed` (`core/application/repositories.py:372`), SQLite impl `sqlite_repositories.py:1315-1320` (`SELECT 1 FROM processed_messages WHERE rule_id/chat_id/message_id`).
- Write: `mark_processed` (`sqlite_repositories.py:1322-1329`, `INSERT OR IGNORE … media_group_id, ts`). 30-day retention sweep `cleanup()` at `sqlite_repositories.py:1337-1340`; per-rule `purge_before` at `sqlite_repositories.py:1331-1335`.
- Keyed `(rule_id, chat_id, message_id)` — **rule-scoped, not target-scoped**. Album events collapse to the first payload's id (the aggregator flushes the first message with `album_messages` set, `dispatcher.py:408`), so one album is one replay-protection row.
- `mark_processed` is also written by the FORWARD_INTERVAL skip path (`pipeline.py:1058-1063`) so a skipped slot is never re-consumed by a replay, and gated at `pipeline.py:1366-1393` so a partial fan-out stays replayable (audit finding #4, comment at `pipeline.py:1355-1365`).
- Status: `FULLY_IMPLEMENTED`.

### DUP-2 Content fingerprint dedup (`duplicate_filters`)
- Property: `ForwardRule.duplicate_filters` — `core/domain/entities.py:922-929` (metadata default False).
- Fingerprint: `_content_fingerprint` (`pipeline.py:267-282`): `sha256( " ".join((text||caption).split()).casefold() + "|" + media_type[:album:<media_group_id>] )`. Whitespace-collapsed + casefolded body, media keyed by **kind + album id, never binary content** (the pipeline never downloads media at filter time, `pipeline.py:271-272`). Rule-scoped: same text under another task is a different fingerprint.
- Claim protocol: `pipeline.py:861-886`. Prefers `self._map.claim_content_hash`; a store without that method falls back to read-only `was_content_forwarded` (`pipeline.py:866-870`).
- Atomicity: SQLite `INSERT OR IGNORE … rowcount == 1` (`sqlite_repositories.py:1885-1897`) on `UNIQUE(rule_id, content_hash)`; Postgres `INSERT … ON CONFLICT (rule_id, content_hash) DO NOTHING RETURNING 1` (`postgres_repositories.py:911-921`). The index, not a read-then-write, picks the winner.
- Release: `_release_content_claim` (`pipeline.py:752-776`) runs in the `finally` of the per-rule loop (`pipeline.py:734-738`) → `release_content_hash` (`sqlite_repositories.py:1899-1904`, `postgres_repositories.py:923-926`). The claim is **earned** only when `enqueued_any` (`pipeline.py:1398-1399` drops the claim without deleting the row). The comments at `pipeline.py:752-761` and `pipeline.py:855-860` explain the past silent-drop bug: recording the claim without ever giving it back marked content as forwarded that never reached a target, so a later legitimate re-post was silently dropped.
- `purge_content_hashes` / `purge_content_hashes_all` (`sqlite_repositories.py:1878-1883`, `1906-1910`) exist for retention.
- Status: `FULLY_IMPLEMENTED`.

### DUP-3 `dedupe_policy` (STRICT / RELAXED / DISABLED) — `STORED_BUT_UNUSED`
- Stored: first-class column `core/domain/entities.py:244` (`dedupe_policy: str = "STRICT"`), DDL `core/infrastructure/persistence/database.py:353` and `postgres_repositories.py:378`, round-tripped by SQLite save/load (`sqlite_repositories.py:364, 388, 654`), gRPC (`grpc_server/servicers.py:238, 482, 1055, 1189-1190`) and backup/restore (`core/application/backup_service.py:201, 601`).
- **No runtime consumer**: grep across `core/` shows zero reads of `dedupe_policy` in `pipeline.py`, `durable_queue.py`, `dispatcher.py`, `services.py` or any engine. The three values STRICT/RELAXED/DISABLED are declared in the comment at `entities.py:244` and nowhere else. The actual dedup behavior is governed by the independent `duplicate_filters` (DUP-2) and `skip_history` (DUP-1) flags. Switching `dedupe_policy` to `DISABLED` or `RELAXED` changes nothing at runtime. Status: `STORED_BUT_UNUSED` (recommended fix PR: either wire it to the DUP-2 gate or remove the column).

### DUP-4 `DuplicateDetectionService` — `UNREFERENCED_HELPER` (production)
- `core/domain/services.py:678-708`: in-memory TTL (300s) / max-size (10000) cache, key `rule_id:chat_id:message_id:target:orig_chat:orig_msg` (`services.py:686-690`).
- Only production-adjacent reference found is the test `core/tests/test_smart_forwarding_rules.py:11,22` (`DuplicateDetectionService(ttl_seconds=60)`). No call from `pipeline.py`, `durable_queue.py` or `dispatcher.py` in the durable path. Status: `UNREFERENCED_HELPER` — the durable path uses the persisted stores (DUP-1/2) and the delivery fence instead.

### DUP-5 Go Worker side — dedup is absent; the fence is the only dedup
- `worker/internal/engine/delivery/delivery.go` has no content-hash or `processed_messages` equivalent (grep for `dedup|content_hash|duplicate` across `worker/` returns only fence/album-split/branding comments). The comment at `delivery.go:203-205` states the design: ingest-side content policies are "baked into the job payload"; the worker re-evaluates **only the attached FilterRule** (`delivery.go:209-218`).
- Send-side idempotency is `FenceClaim` (`delivery.go:238-285`) on `job.AttemptKey{RuleID, SourceChatID, SourceMessageID, TargetChatID, JobKind}` (`delivery.go:238-244`). `FenceAlreadySent` → mark SENT without re-sending (`delivery.go:256-273`); `InFlight/Stale/Unknown` → defer 15s (`delivery.go:274-282`). Mirror of the Python legacy fence at `dispatcher.py:524-570` (`DeliveryAttemptKey` imported from `core/application/repositories.py`).
- Consequence: **content-level dedup is Python-only**. Duplicate content forwarded once by Python is deduped at enqueue; a job already in the queue is deduped at the fence. See parity finding P-1.

---

## 4. Forward Every N (FEN-*)

### FEN-1 Arithmetic — `core/domain/forward_interval.py` (100 lines, whole module)
- `DEFAULT/MIN/MAX_FORWARD_EVERY_N = 1/1/1000` (`forward_interval.py:42-44`).
- `clamp_forward_every_n` (`forward_interval.py:47-71`): None/bool/garbage/NaN → 1; below min → 1; above max capped at 1000. Failing open keeps the pre-feature behaviour.
- `owns_slot(sequence, every_n)` (`forward_interval.py:74-85`): 1-based; with N=5 the messages 1, 6, 11… are forwarded (`(sequence-1) % every_n == 0`); **non-positive sequence never owns a slot** (`forward_interval.py:83-84`) so an unreadable counter cannot forward twice in a row.
- `slots_until_next` (`forward_interval.py:88-100`): UI helper only, never decides delivery.

### FEN-2 Storage — `rule_counters`
- Property `ForwardRule.forward_every_n` (`core/domain/entities.py:344-369`): reads through `clamp_forward_every_n` on read **and** write; the default (1) leaves no metadata trace so "off" and "never configured" stay indistinguishable and backups stay byte-identical (`entities.py:364-367`).
- `IRuleCounterRepository.advance` (`core/application/repositories.py:386-409`): one atomic statement; the docstring at `repositories.py:390-401` explains why the count must not be derived from `processed_messages` (retention sweeps) or delivery jobs (per-target).
- SQLite impl `SqliteRuleCounterRepository` (`sqlite_repositories.py:1343-1369+`): `INSERT … ON CONFLICT(rule_id) DO UPDATE SET accepted = CASE WHEN rule_counters.every_n = excluded.every_n THEN accepted + 1 ELSE 1 END` — the increment **and** the reset-on-interval-change are one SQL expression inside the DB lock, so two concurrent workers cannot both read `accepted = 4`.

### FEN-3 Pipeline gate — `pipeline.py:1025-1084`
- Position: **after every filter (R9-R12) and after the content-dedup claim (R6), before TRANSFORM (R15)** — comments at `pipeline.py:1026-1029`. A message the task would not forward never consumes a slot, and a message that will not be sent does not pay for an AI rewrite.
- Conditions: `every_n > 1` **and** `not payload.is_edit` **and** `self._counters is not None` (`pipeline.py:1039`). Albums consume exactly one slot because the aggregator hands the pipeline one payload (documented at `forward_interval.py:19-22`). Edits are never counted (`forward_interval.py:22-24`, enforced at `pipeline.py:1037-1038`) — an edit updates a copy that already had its slot.
- `advance` failure is **fail-open**: the message is forwarded anyway (pre-feature behaviour) and the failure logged at ERROR (`pipeline.py:1045-1053`).
- On a skip: the message is marked processed (`pipeline.py:1058-1063`) so a replay cannot consume the next slot, `FORWARD_INTERVAL` records `every_n_skip`, metrics bump, `forward.skipped`/`FORWARD_INTERVAL` audit (`pipeline.py:1069-1075`), and the rule returns.
- N=1 no-op: `every_n > 1` is false, so the whole block is skipped; `owns_slot` also short-circuits to True for `every_n <= 1` (`forward_interval.py:81-82`). Without a `_counters` wiring the setting is inert and every message is forwarded (`pipeline.py:358-362`).
- Status: `FULLY_IMPLEMENTED`.

---

## 5. Keyword confirmation / reply (KW-*)

### KW-1 Storage
- `ForwardRule.keyword_confirmation` — `core/domain/entities.py:1787-1796` (metadata, default False).
- `ForwardRule.keyword_reply` — `core/domain/entities.py:1799-1809` (metadata, default `""`).
- Keyword list: **`rule.whitelist`** — `classify_keyword_trigger` reads `rule.whitelist` as the keyword list (`services.py:1514`). This is the historical double-meaning documented in switch-matrix row 08 (`docs/task-feature-switch-matrix.md:21`): the bot-menu "whitelist" and the keyword list are the same storage.

### KW-2 Decision — `AdvancedSettingsEngine.classify_keyword_trigger`
`core/domain/services.py:1494-1521`. Gate order (all must pass):
1. `rule.keyword_confirmation` else None (`services.py:1505-1506`)
2. reply text non-empty after `.strip()[:KEYWORD_REPLY_MAX_LEN]` (`services.py:1507-1509`)
3. `rule.is_addon_enabled(KEYWORD_REPLY_ADDON_KEY)` — the paid-add-on entitlement (`services.py:1510-1511`)
4. at least one keyword in `rule.whitelist` (`services.py:1513-1515`)
5. **substring, case-insensitive** match of any keyword in the lowercased haystack (`services.py:1519-1520`) — no word-boundary, no regex, no per-interval counter.

The docstring at `services.py:1517-1518` records the contract: a keyword confirmation **never drops the forwarded message**, it only decides whether the reply is also posted.

### KW-3 Pipeline call and enqueue — `pipeline.py:1298-1346`
- Runs **after** the primary per-target enqueue loop, deliberately: "a reply failure can never drop or delay the forward itself; the reply is a secondary, best-effort side effect" (`pipeline.py:1298-1302`).
- Input text: `payload.effective_text or ctx.transformed_text or ""` (`pipeline.py:1305`) — i.e. the **source text is preferred over the transformed text**.
- Enqueue: `self._queue.enqueue_delivery(rule, payload, target, reply_text, eval_res, job_kind="keyword_reply", correlation_id=...)` for every target in `delivery_targets` (`pipeline.py:1312-1323`). Note `delivery_targets` already excludes C when the hop is enabled, with the rationale at `pipeline.py:1308-1311`: a keyword reply is our own plain text carrying no forward header to brand.
- Job id and uniqueness: `job_kind` produces a distinct job id `<rule>:<chat>:<msg>:<target>:keyword_reply` (`durable_queue.py:415-416`) and is part of the delivery_jobs UNIQUE key; `payload_data["is_reply"] = job_kind != "forward"` (`durable_queue.py:376`).
- A refused enqueue logs a warning naming the target (`pipeline.py:1324-1331`) — the fix for the historical "the log claimed the reply shipped" bug. Any exception here is swallowed with a warning so the forward stands (`pipeline.py:1340-1346`).

### KW-4 Historical UNIQUE-key bug and fix (switch-matrix rows 25/26)
`docs/task-feature-switch-matrix.md:38-39` records: the reply could not be stored because `delivery_jobs` deduped on `UNIQUE(rule, source_chat, source_message, target)` without the kind, so the reply's `INSERT OR IGNORE` matched the forward's row and was dropped in silence while the pipeline logged success; the reply outcome also overwrote the forward's `message_map` row, breaking edit/delete sync. Fix (per the matrix): `job_kind` is now a column **and** part of the queue's UNIQUE key (SQLite rebuild inside a SAVEPOINT; Postgres constraint widened), the pipeline logs refusals, and only the forward writes the mapping row. Verified in current source: `durable_queue.py:408-416` (kind in the id), `durable_queue.py:427` (`job_kind=job_kind` on the DeliveryJob), and the enqueue docstring at `durable_queue.py:288-291`. Row 26 ("Keyword Reply Threshold") is `PARTIAL`: the gate (entitlement + truncation) is enforced but **there is no per-interval counter** — "threshold" currently means "keyword must match", not a rate. Status: KW-1..KW-3 `FULLY_IMPLEMENTED`; the threshold counter is `NOT_IMPLEMENTED`.

---

## 6. Branding, pipeline side (BRD-*)

### BRD-1 Domain config and decision — `core/domain/branding.py`
- Config store: `metadata["branding_hop"]` (`BRANDING_META_KEY`, `branding.py:51`), normalised by `read_config` (`branding.py:94-110`); defaults `triggers_enabled=False, require_forward=False, force_forward=True, match_mode="any"` (`branding.py:58-67`); `write_config` (`branding.py:113-127`).
- `channel_id` (`branding.py:133-135`) reads `rule.intermediate_channel_id`.
- `enabled` (`branding.py:138-152`): requires a channel **and** either `use_intermediate` or a legacy `intermediate_mode` in `("VIA_INTERMEDIATE", "MASK_ORIGIN")` (`branding.py:56`). Both spellings count, otherwise a rule switched on through one spelling silently delivered directly.
- `match_origin` (`branding.py:177-200`): matches Telegram's **forward origin** (`forward_origin.from_chat_id` / `from_chat_username` / `from_chat_title`), substring + case-insensitive on titles — never the message body.
- `match_keywords` (`branding.py:203-223`): delegates folding to `content_policy.fold_text` so Arabic/Persian letter spellings and invisible characters match.
- `should_hop` (`branding.py:226-281`): no channel → `branding_disabled:no_channel`; not enabled → `branding_disabled:flag_off`; triggers off → legacy VIP-criteria behaviour else `branding_always` (`branding.py:245-252`); `require_forward` is a **gate**, not a check (`branding.py:254-256`); match_mode `all` requires every configured condition, `any` at least one (`branding.py:277`); no conditions configured → `branding_no_triggers_configured` hops (`branding.py:273-275`).

### BRD-2 Pipeline-side decision
- The durable pipeline does **not** call `branding.should_hop` directly. It calls `self._branding_hop.branding_decision(payload, rule)` (`pipeline.py:1140`) — the dispatcher's public wrapper (`dispatcher.py:609-615`), which delegates to `_brand_hop_decision` (`dispatcher.py:706-719`): it prefers `use_case._routing.should_brand_hop` (the SmartRoutingEngine path) and falls back to the module-level `branding.should_hop` only when no service is wired. One decision function for the live, queue, preview and bot paths.
- Decision failure is non-blocking: `hop_enabled, hop_reason = False, "branding_decision_failed:…"` (`pipeline.py:1141-1142`).
- Geometry checks (`pipeline.py:1143-1160`): C removed from `targets` to form `delivery_targets`; hop disabled (direct delivery, warning logged) when C is empty, when nothing but C remains, or when **C == the message's own source** (`pipeline.py:1147-1157`).

### BRD-3 Hop execution / intent materialization
- Two modes, keyed on `_worker_owns_execution()` (`pipeline.py:532-541`, env `ATF_WORKER_ENABLED` in 1/true/yes/on):
  - **Worker-owns mode**: `_branding_intent(rule, hop_reason, target_text)` (`pipeline.py:543-578`) builds an INTENT plan — `{chat_id, message_id: 0, message_ids: [], force_forward, content_mode, reason, dedup_key, stage: "INTENT"}` — and **posts nothing**. The Go Worker performs stage 1 (`delivery.go:960-1037`), checkpointing intermediate ids (`delivery.go:1028-1036`) and reusing them on retry (`delivery.go:986-992`). An ambiguous stage-1 failure marks the job UNKNOWN rather than re-posting (`delivery.go:1005-1011`).
  - **Python-owns mode**: `_perform_brand_hop` (`pipeline.py:501-526`) calls the dispatcher's `perform_branding_hop` (`dispatcher.py:617-678`) → `branding_hop.post_to_channel` with re-upload fallbacks (`dispatcher.py:651-660`). Failure or "nothing posted" returns None and the caller delivers directly — never drops (`pipeline.py:522-525`, `dispatcher.py:670-675`).
- **Dedup of hop plans per distinct text**: `hop_plans: Dict[str, Optional[Dict]]` (`pipeline.py:1161`) is keyed on `target_text or ""`, so several targets sharing one processed text share one post in C, while targets whose transforms differ each get their own (`pipeline.py:1230-1254`, rationale at `pipeline.py:1230-1234`). In worker-owns mode this is backed by the content fingerprint `dedup_key`.
- `_branding_fingerprint` (`pipeline.py:580-603`): `sha256(rule.id ␟ channel_id ␟ content_mode ␟ album ␟ normalize_for_fingerprint(text))` over `"\u241F"`-joined parts; the text digest is computed by `_normalize_for_fingerprint` (`pipeline.py:285-302`), which applies `content_policy.fold_text` and collapses whitespace so a rewrap is not a new post.
- The job carries the plan as `payload_data["branding"]` and as the job's `intermediate_chat_id` / `intermediate_message_id` / `delivery_stage="BRANDED"` columns (`durable_queue.py:428-433`), so a replay never posts a second copy into C (`durable_queue.py:299-301`).
- Go-side hop identity: `hopIdentity` (`worker/internal/engine/delivery/branding_stage.go:170-177`) = `hop:<ruleID>:<dedupKey>` — one logical post per (event, content fingerprint), while each target's stage-2 delivery keeps its own fence row (`branding_stage.go:166-169`).
- Status: `FULLY_IMPLEMENTED`.

### BRD-4 Worker ownership boundary (`_worker_owns_execution`, `pipeline.py:532-541`)
- Checks only the env var `ATF_WORKER_ENABLED` (1/true/yes/on). The Python queue loop stands down symmetrically at `DurableQueueManager.start` (`durable_queue.py:199-215`): the loop does not start, but **enqueue stays Python's**. What moves: delivery execution, the branding stage-1 side effect, rate limiting, the fence, auto-delete scheduling. What stays in Python: every filter, every transform, per-target overrides, the hop *decision*, and the durable enqueue.

---

## 7. Albums

### ALB-1 Aggregation — `AlbumAggregationService` (`core/infrastructure/telegram/album_aggregator.py`)
Keyed `(session_id, chat_id, media_group_id)` (`album_aggregator.py:72`). Bounded LRU (max 500 active albums, `album_aggregator.py:91-98`) + TTL flush (constructor default 2.0s, `dispatcher.py:96`) + immediate flush at 10 items (Telegram's album cap, `album_aggregator.py:83-88`) + dedup of a repeated message id inside one group (`album_aggregator.py:37-38`). `stop()` flushes pending groups (`album_aggregator.py:134-144`).

### ALB-2 Flush into the pipeline — `_flush_album` (`dispatcher.py:406-414`)
`first_msg.album_messages = list(all_msgs)` then `durable_pipeline.process(first_msg, "NEW_MESSAGE")`. So the album is a **single pipeline event**: one fingerprint (with `:album:<group_id>` suffix, `pipeline.py:279-280`), one `forward_every_n` slot, one `processed_messages` row, one dedup claim per rule.

### ALB-3 Enqueue and delivery
`enqueue_delivery` serialises `album_message_ids` (`durable_queue.py:330-332`, fallback `[payload.message_id]`) and `album_mode` (`durable_queue.py:347`). Python dispatch (`durable_queue.py:2116-2213`): `"first"` copies only the first item, `"split"` copies each item **resuming from the `album_sent` map** (`durable_queue.py:2140-2173`, progress persisted by `_record_album_progress` at `durable_queue.py:980`), default `"album"` uses `copy_media_group` with a re-upload fallback (`durable_queue.py:2196-2213`). Go dispatch (`delivery.go:574-634+`) implements the same three modes with the same resume semantics (audit finding E2E-5, `delivery.go:570-573`) and keys the mapping on the **first** item of the group (`delivery.go:632-634`).

### ALB-4 Settings
`ForwardRule.album_mode` — `entities.py:817-825` (metadata, default `"album"`). `album_aggregation_window_seconds` — `entities.py:410` (default 0.8) is **not read by the dispatcher** (which hard-codes 2.0s at `dispatcher.py:96`); see P-4.

---

## 8. Parity findings (Python vs Go Worker)

Each finding names the minimal fixture, the two outputs, the expected behaviour, severity, and a recommended *separate* fix PR.

### P-1 — Content-level dedup exists only in Python (`HIGH`)
- **Fixture**: rule R with `duplicate_filters=true`; two source messages in chat A with different ids and byte-identical text "hello" (a re-post), target B. `ATF_WORKER_ENABLED=1`.
- **Python output**: message 2 hits `claim_content_hash` → already=True → DEDUPLICATE `duplicate_content`, no job (`pipeline.py:861-886`). Never enqueued.
- **Go output**: `worker/internal/engine/delivery/delivery.go` has no content-dedup path (grep: no `content_hash`/`dedup` store). Because both messages were *accepted* by Python only once, the worker never sees the second; but a job produced by a **replay** (e.g. a replayed dead-letter event, or two rules sharing a target) bypasses the fingerprint entirely and the fence (`delivery.go:238`) keys on `(rule, source chat, source msg, target, kind)` — different source ids, so both send.
- **Expected**: identical content under the same task should not be delivered twice, whichever engine executes.
- **Severity**: HIGH (user-visible duplicate delivery, and the reason the claim protocol exists).
- **Recommended fix PR**: port the claim/`content_dedup` store to the worker (or record the content hash on the job at enqueue and have the worker fence on it). Separate from the pipeline audit.

### P-2 — Keyword reply text is matched on the source text, not the transformed text (`MEDIUM`)
- **Fixture**: rule R with `keyword_confirmation=true`, `keyword_reply="got it"`, `whitelist=["sale"]`, and a transform that rewrites the body (e.g. a replacement "sale" → "offer"). Source text "big sale today".
- **Python output**: `classify_keyword_trigger(payload.effective_text or ctx.transformed_text or "", rule)` (`pipeline.py:1304-1305`) prefers `payload.effective_text`, so the keyword matches **"sale"** and the reply is enqueued even though the delivered text no longer contains it.
- **Go output**: n/a — the reply is a pure-text job; the worker only sends `p.Text()` (`delivery.go:367-386`).
- **Expected**: either the match runs on the same text the target receives, or the precedence is a documented choice. Today the code comment says the reply is decided "after the primary enqueue" (`pipeline.py:1298-1302`) but says nothing about which text, and the `or` fallback hides the precedence.
- **Severity**: MEDIUM (behavioural, easily mistaken for a bug by an operator; no data loss).
- **Recommended fix PR**: match on `ctx.transformed_text` (or make the precedence explicit with a setting).

### P-3 — `dedupe_policy` (STRICT/RELAXED/DISABLED) is inert (`MEDIUM`)
- **Fixture**: rule R with `dedupe_policy="DISABLED"` and `duplicate_filters=true`; a re-post of identical text.
- **Python output**: the fingerprint claim still fires (`pipeline.py:861` reads only `duplicate_filters`); message 2 dropped as `duplicate_content`.
- **Go output**: n/a (Python-only gate, as with P-1).
- **Expected**: `DISABLED` should disable the dedup that the column's own comment (`entities.py:244`) promises.
- **Severity**: MEDIUM (a stored, surfaced, documented setting that does nothing — an operator cannot turn dedup off).
- **Recommended fix PR**: wire `dedupe_policy` into the `pipeline.py:861` gate, or drop the column and the gRPC/backup plumbing.

### P-4 — Album aggregation window: 0.8s setting vs 2.0s runtime (`LOW`)
- **Fixture**: rule R with `album_aggregation_window_seconds=0.8` (the documented default, `entities.py:410`); a media group whose items arrive 1.5s apart.
- **Python output**: `AlbumAggregator(flush_callback=…, ttl_seconds=2.0, max_active_albums=500)` (`dispatcher.py:96`) — the rule's 0.8s is never read, so the group still collects at 2.0s and the album flushes as one event.
- **Go output**: n/a (aggregation is Python-only; the worker receives the already-aggregated job).
- **Expected**: the task's configured window governs, as the property's existence implies.
- **Severity**: LOW (worst case the album splits into two deliveries, and 2.0s is the more conservative choice).
- **Recommended fix PR**: pass `rule.album_aggregation_window_seconds` (or a global) into the aggregator's TTL.

### P-5 — `_handler` autoreply branch is non-terminal for the v27 engine (`LOW`)
- **Fixture**: a private-chat message handled by `autoreply_engine` (not the legacy `pv_responder`).
- **Python output**: `handle_incoming` is awaited (`dispatcher.py:345`) but the handler does **not** return, so the message also flows into `durable_pipeline.process` and is forwarded by any matching rule. Only the legacy path returns after replying (`dispatcher.py:353-357`).
- **Go output**: n/a.
- **Expected**: documented intent (the v27 engine replies asynchronously while the forward proceeds — plausible by design, since a PV forward rule and an auto-reply are different concerns) — but the two branches differ in termination semantics for the same trigger, which is the kind of asymmetry the brief's outline assumed away.
- **Severity**: LOW (informational; verified behaviour, likely intended).
- **Recommended fix PR**: document the asymmetry at `dispatcher.py:331-335`, or unify both paths to return or not return.

### P-6 — `DuplicateDetectionService` is an unused in-memory dedup (`LOW`)
- **Fixture**: any message; the durable path.
- **Python output**: never consulted (no production caller; only `core/tests/test_smart_forwarding_rules.py:22`).
- **Go output**: n/a.
- **Expected**: either the service is wired (giving a fast in-memory pre-filter) or removed so it is not mistaken for a dedup layer.
- **Severity**: LOW (dead code that implies a safety net that does not exist).
- **Recommended fix PR**: remove the class or wire it behind the persisted claims as a cheap short-circuit.

---

## 9. Fixtures proposed

| id | one-line description |
|---|---|
| `FIX-C-01` | New channel message → one matching rule → assert the exact stage_result order INGEST, NORMALIZE, DEDUPLICATE, FILTER, FORWARD_INTERVAL, TRANSFORM, ENQUEUE with the recorded reasons. |
| `FIX-C-02` | Edit of an already-delivered (mapped) message → assert `_route_edit` claims it and no pipeline enqueue happens; edit of an unmapped message → forwarded exactly once. |
| `FIX-C-03` | `duplicate_filters=true` + identical text under two message ids → second is dropped; then a message that is filtered *after* the claim → assert `release_content_hash` was called (claim does not stick). |
| `FIX-C-04` | `forward_every_n=3` with three messages → assert `advance` returned 1,2,3, only #1 and #3 enqueue, and the skipped #2 was written to `processed_messages`. |
| `FIX-C-05` | Keyword match with `job_kind="keyword_reply"` → assert two `delivery_jobs` rows for the same (rule, source, target) and that the reply outcome did not touch the forward's `message_map` row. |
| `FIX-C-06` | Hop enabled with two targets sharing one transformed text → assert exactly one post into C (`hop_plans` size 1); then enable `ATF_WORKER_ENABLED` → assert the plan is an INTENT with `message_id=0` and a non-empty `dedup_key`. |
| `FIX-C-07` | Media group of 3 items → assert one pipeline event, one `forward_every_n` slot, `album_message_ids` of length 3 on the job, and `album_mode=split` resume after a mid-album failure. |
| `FIX-C-08` | Reply in source chat to a previously delivered message with `sync_replies=true` → assert the reply is posted as a reply to the mapped copy and the event is claimed (not re-forwarded); switch off → falls through to forwarding. |
| `FIX-C-09` | Backfill: a channel with a truncated difference → missing ids recorded as gaps → replayed through `message_handler` → assert `_resolve_ingestion_gap` cleared the row before filtering. |
| `FIX-C-10` | `ATF_WORKER_ENABLED=1` → assert the Python queue loop did not start while `enqueue_delivery` still writes jobs, and that the hop plan on the job is an INTENT. |

---

## 10. Unknowns

1. **`album_aggregation_window_seconds` wiring** — the property exists (`entities.py:410`) but I found no read of it in `dispatcher.py`, `album_aggregator.py` or `pipeline.py`. Either a surface elsewhere reads it (not verified) or it is `STORED_BUT_UNUSED`; I recorded it as P-4 rather than asserting either.
2. **`SmartRoutingEngine.should_brand_hop`** (used by `_brand_hop_decision`, `dispatcher.py:714-716`) — I verified the delegation but did not audit the engine's own VIP/detection-criteria logic (`services.py:560-603` is Agent A's `RT-*` scope); the exact reason strings Python produces for `hop_reason` under worker ownership are therefore `NOT_VERIFIED` beyond `branding.py`'s own set.
3. **`FenceClaim` store semantics** — the Go call is at `delivery.go:246` and the outcomes are handled at `delivery.go:255-285`, but the store implementations (`worker/internal/queue/postgres.go`, sqlite) are only cited, not line-audited; the exact stale-claim sweep cadence is `NOT_VERIFIED`.
4. **Multi-replica lease coordination** — `DurableQueueManager.start` mentions a singleton guard (`durable_queue.py:228`) and redis-backed leases (`durable_queue.py:246`), but the Redis path's availability/behaviour was not exercised in this audit.
5. **Postgres `claim_content_hash` under read-replica lag** — the `RETURNING 1` form (`postgres_repositories.py:914-921`) is atomic on the primary; whether deployments route reads through replicas is a deployment fact, `NOT_VERIFIED`.
6. **`keyword_reply` on the legacy direct path** — the reply is enqueued through `self._queue.enqueue_delivery` (`pipeline.py:1319`); when `self._queue is None` the loop `break`s (`pipeline.py:1313-1314`), i.e. the standalone/no-queue mode sends no reply at all. Whether any deployment runs without a queue manager is `NOT_VERIFIED`.
