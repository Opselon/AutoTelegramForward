# Agent C — Routing, Pipeline & Event Lifecycle

## Mission
Stage 4.0 discovery-only audit of AutoTelegramForward (repo: /root/AutoTelegramForward, branch `audit/complete-business-logic-inventory`, baseline master `35ee530`). One of four parallel agents. **Audit only — do NOT rewrite engines, change schemas, or change runtime behavior.**

## Your scope (feature prefixes)
`EVT-*` (event lifecycle), `DUP-*` (dedup), `FEN-*` (Forward Every N), `KW-*` (keyword reply), `BRD-*` (pipeline-side branding), album handling, and the **canonical processing-order map** for every path.

## Hard rules
- Cite `file:line` for every behavioral claim.
- Never edit any file outside `docs/behavior-audit/_handoff/c-routing-pipeline.md`.
- Never edit `docs/feature_catalog.json` or another agent's handoff.
- Never write Go/Python production code; never `git commit`/`git push`.
- The processing order is DERIVED FROM SOURCE, never assumed. The brief's §12 outline is illustrative and may be wrong — verify and correct it.

## Required deliverable
ONE file: `docs/behavior-audit/_handoff/c-routing-pipeline.md` with these sections:

### 1. Event lifecycle (EVT-*)
Map every Telegram update type through the system. Entry: `core/infrastructure/telegram/dispatcher.py:313` `_make_handler`. Document the branch order in `_handler` (dispatcher.py:314-404):
1. AI PV auto-reply for private chats (NEW_MESSAGE + private chat) — dispatcher.py:325-348
2. EDITED_MESSAGE → `_route_edit` (dispatcher.py:369, def at 273) — if mapped, edit target in place and RETURN; else fall through as first delivery
3. NEW_MESSAGE with `reply_to_message_id` → `_route_reply` (dispatcher.py:384, def at 290)
4. Album: `payload.media_group_id` → `self._album_aggregator.ingest` (dispatcher.py:393) then RETURN; flush at `_flush_album` (406)
5. else `durable_pipeline.process(payload, trigger)` or use_case fallback (dispatcher.py:399)
Deleted messages: `_make_deleted_handler` at dispatcher.py:237.
Then for EACH event type give the full path with line refs:
- new message, edited message, edited caption, deleted message, reply, album/media group, service event, historical/backfill, polling-recovered.
Read `core/infrastructure/telegram/sync_engine.py` for edit/delete/reply mapping (`message_map`), and `core/infrastructure/telegram/channel_polling.py` for the polling path. Read `core/application/pipeline.py:633` `process()` for the pipeline entry and stage ordering.

### 2. Canonical pipeline stage order (the critical deliverable)
Read `core/application/pipeline.py` `_process_for_rule` (778) and `process` (633) end to end. Verify and correct this order with exact line numbers:
rule paused/auto-pause (786) → since_ts too-old (802) → ignore_edits (813) → edit-time window (820) → skip_history duplicate-event (836) → content dedup claim (~860) → forward_every_n (FORWARD_INTERVAL) → FilterEngine.evaluate (918) → session time-limited (938) → media/link block reasons (957) → content policy chain task+source (974-1010) → [transforms, see Agent B/D] → per-target loop: loop-skip (1162) → target policy evaluate+transform (1197-1214) → branding (1216+) → durable enqueue (1240+). Document what happens at each stage on failure/skip, and which stages are event-level vs per-rule vs per-target.

### 3. Deduplication (DUP-*)
- `DuplicateDetectionService` at `core/domain/services.py:678`.
- Content fingerprint: `_content_fingerprint` at `pipeline.py:267` and `_normalize_for_fingerprint` (285) — what exactly is hashed (body + media kind, rule-scoped).
- The claim/release protocol: `claim_content_hash` and `_release_content_claim` at `pipeline.py:752` — document the atomicity (UNIQUE(rule_id, content_hash)) and why claims are released when delivery is rejected (the comments explain a past silent-drop bug).
- `duplicate_filters` setting (entities.py:713) and `dedupe_policy` (STRICT/RELAXED/DISABLED, entities.py:244).
- `skip_history` + `processed_messages` store (`was_processed`).
- Go side: does the worker dedupe? Check `worker/internal/engine/delivery/delivery.go` and the fence (`FenceClaim`, `pipeline.py`-adjacent queue code in `core/infrastructure/telegram/durable_queue.py`).

### 4. Forward Every N (FEN-*)
`core/domain/forward_interval.py`, `ForwardRule.forward_every_n` property (entities.py:136-160, clamped 1-1000), the pipeline stage, `IRuleCounterRepository.advance` (atomic UPSERT, `rule_counters` table). Document: where the gate sits relative to filters and album aggregation (albums consume one slot; filtered messages consume none), N=1 no-op behavior.

### 5. Keyword confirmation / reply (KW-*)
`AdvancedSettingsEngine.classify_keyword_trigger` at `core/domain/services.py:1477` (reads `keyword_confirmation`, `keyword_reply`, `whitelist` as keyword list, add-on gate `is_addon_enabled`). Document where the pipeline calls it and how the reply is enqueued (`job_kind="keyword_reply"`), plus `keyword_reply` storage (entities.py:1590) and `keyword_confirmation` (1578). Note from the switch matrix (docs/task-feature-switch-matrix.md rows 25/26) the historical UNIQUE-key bug and its fix.

### 6. Branding pipeline side (BRD-*)
`core/domain/branding.py` (`read_config` 94, `enabled` 138, `should_hop` 226, `match_origin` 177, `match_keywords` 203), `pipeline.py:543` `_branding_intent`, `pipeline.py:501` `_perform_brand_hop`, `pipeline.py:532` `_worker_owns_execution`, hop decision in `_process_for_rule` (~1100-1150), dedup of hop plans per distinct text (`hop_plans` dict, pipeline.py:1150). Branding Go side is Agent D's `DLV-*`; you document only the pipeline-side decision and intent materialization.

### 7. Albums
`AlbumAggregationService` at `core/domain/services.py:1300` (bounded LRU + TTL, `album_aggregation_window_seconds` entities.py:410, default 0.8s), `_flush_album` at dispatcher.py:406, album handling in enqueue (album media-ids), `album_mode` (entities.py:609: "album" default).

### 8. Go worker ownership boundary
`pipeline.py:532` `_worker_owns_execution()` — what it checks (config/feature flag), and what moves to the worker vs stays in Python when enabled. `worker/internal/engine/delivery/delivery.go` claim→preflight→filter→rate-limit→fence→branding-stage-1→dispatch→mapping→auto-delete (lines 190-360).

### 9. Parity findings + fixtures + unknowns
Each finding: minimal input fixture, Python output, Go output, expected, severity, recommended separate fix.

## Starting points
`core/infrastructure/telegram/dispatcher.py`, `core/application/pipeline.py`, `core/domain/services.py` (678-1337, 1409-1567), `core/domain/forward_interval.py`, `core/domain/branding.py`, `core/infrastructure/telegram/sync_engine.py`, `core/infrastructure/telegram/durable_queue.py`, `worker/internal/engine/delivery/delivery.go`.
