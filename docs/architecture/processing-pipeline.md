# Processing Pipeline — source-verified execution map

**Baseline:** master `35ee530cc80cd17d1ac7d702c4e5f5ea8024889a`. Audit-only.

Machine-readable twin: `pipeline-order.json`. Diagrams: `diagrams/*.mmd`.

## Paths

Five distinct paths exist. They are not variations of one flow — they have different
gates, different transform code, and different failure semantics.

### A — New message, durable path (the production path)

`Telegram Update` → `dispatcher.py:314 _handler`
1. **INGEST** — payload normalization (`dispatcher.py:316`); a parse defect returns.
2. **NORMALIZE** — gap resolution (`pipeline.py:659-660`) runs **before any filtering**.
3. PV auto-reply (private chats, `dispatcher.py:325-348`) — non-terminal for the v27 engine.
4. EDITED branch (`dispatcher.py:369` → `_route_edit` :273) — mapped → edit in place + return.
5. REPLY branch (`dispatcher.py:384` → `_route_reply` :290).
6. ALBUM branch (`dispatcher.py:393` → `AlbumAggregator.ingest`, flush :406).
7. `pipeline.process()` (`pipeline.py:633`) — service-message drop (:681-685).
8. **ROUTING** — `matching_rules`, per-owner single match (`services.py:622-655`).
9. `trigger_events` filter (`pipeline.py:690`).

**Per-rule gates** (`_process_for_rule`, `pipeline.py:778-1399`) — every gate reads the
`materialize()`-merged view:

| # | Gate | Site | On failure |
|---|---|---|---|
| 0 | **`materialize(bot defaults)`** | `pipeline.py:787` | — (runs first, by design) |
| 1 | paused / auto-paused | `pipeline.py:789` | audit + stop |
| 2 | `since_ts` too-old | `pipeline.py:801` | skip |
| 3 | `ignore_edits` + edit-time window | `pipeline.py:811, 824` | skip |
| 4 | `skip_history` duplicate-event | `pipeline.py:837` | skip |
| 5 | content-dedup claim | `pipeline.py:861` | skip; released in `finally` (:752) |
| 6 | FilterRule fetch + **owner check** | `pipeline.py:892-916` | filter ignored when not owned |
| 7 | `FilterEngine.evaluate` | `pipeline.py:918` | DROP with reason |
| 8 | session time-limited | `pipeline.py:935-949` | **DEFER (enqueue), never drop** |
| 9 | media/link block reasons | `pipeline.py:955` | DROP |
| 10 | content-policy chain (task + source) | `pipeline.py:974-1016` | DROP with reason |
| 11 | `forward_every_n` slot gate | `pipeline.py:1036-1084` | `every_n_skip` |

**Transform** (`_apply_transforms`, `pipeline.py:1401-1580`) — fixed order, unchanged by
configuration. The single drop inside it is `empty_after_clean` (:1467).

1. `CleanerRule.clean` (:1408) — failure never drops (deliver uncleaned, :1437)
2. `LinkManagementEngine.apply_to_payload` (:1448) / `elif link_replacement` (:1456)
3. `remove_emojis` flag path (:1461)
4. `_clamp_entities` (:1464)
5. **empty-after-clean drop** (:1467-1486)
6. `ContentPolicy.transform` (:1496) — failure never drops (:1493)
7. AI transform (:1524) — gated by `ai_config_id` **and** `ai_rewrite_active`
8. `replace_text` (`rule.replacements`, :1543)
9. header/footer (:1547-1552) — **raw; placeholders NOT interpolated** (F-15)
10. watermark / link_buttons / affiliate_kit (:1558)
11. `_clamp_entities` final (:1573)

**Enqueue, per target** (`pipeline.py:1163-1296`): `is_loop` skip → target-scoped policy
evaluate + transform (only `target:`-prefixed, on the transformed text — see the note
below) → branding hop plan (deduped per distinct text) → `enqueue_delivery`.

**After the fan-out:** keyword reply enqueue (:1298, secondary best-effort) →
`mark_processed` gated on the whole fan-out (:1366-1393) → dedup claim earned (:1398).

**Go Worker** (`delivery.go:190-360`): claim → preflight → attached FilterRule
re-evaluate (:212, on the baked payload text) → per-target rate limit (**defer**, :221) →
idempotency fence claim (:238) → branding stage 1 post into C + checkpoint → dispatch
(USER or BOT transport) → `FenceRecordSent` before commit → `scheduleAutoDelete` (durable).

> **The pipeline's own comment on why only the target override is re-decided**
> (`pipeline.py:1186-1196`): re-deciding the task's own policy at enqueue would judge the
> task's own transforms — a header or replacement containing one of the task's own
> blacklist words used to reject **every** message the task ever forwarded, silently.
> Only the target-scoped override, which the FILTER stage never saw, is decided there.

### B — Direct delivery

No durable queue configured (standalone/mock). `pipeline.py:1335` sets `enqueued_any` and
`dispatcher._send` delivers directly through the Pyrogram client. Same gates and
transforms as A; durability and the fence are absent.

### C — Edit / delete / reply synchronization

Edits route at `dispatcher.py:369`; when a `message_map` row exists, `sync_engine` updates
each target in place and processing stops. The **edit sync path re-runs
`FilterEngine.transform_text`** (`sync_engine.py:261`) — the only production caller of the
legacy 9-step transform besides the gRPC preview. So an edit can arrive at the target with
different formatting than the original durable delivery (F-04, F-15).
Deletes never enter the pipeline (`dispatcher.py:237`).

### D — Album

`dispatcher.py:393` ingests into `AlbumAggregator` (bounded LRU, TTL hard-coded 2.0s —
F-02, the per-task `album_aggregation_window_seconds` field is never read). Flush (:406)
attaches `album_messages` and follows the normal durable path. Albums consume **one**
`forward_every_n` slot because the gate sits after aggregation.

### E — Go Worker durable delivery

Re-evaluates **only** the attached FilterRule (`delivery.go:212`) and the per-target
`TargetOverride`. Per `delivery.go:193-199`, ingest-side content policies and transforms
are "baked into the job payload". Two consequences: the Go `transform` package is entirely
unreferenced (F-08), and `BlockLinks` is never populated from the store (F-12).

## Event types

10 mapped: new message, edited message, edited caption (no separate branch —
`sync_engine.py:271-288`), deleted message, reply, album/media-group, service event,
historical/backfill (`channel_polling.py:1082-1119` replays through the identical
`_handler` closure), polling-recovered (same path + dedup at `pipeline.py:837-886`), and
the rule-level `trigger_events` filter (`pipeline.py:690`).

## Failure semantics (verified)

| Stage | Failure behavior |
|---|---|
| Cleaner | never drops — deliver uncleaned + log (`pipeline.py:1437`) |
| ContentPolicy | never drops by crashing — deliver unmodified (`pipeline.py:1493`) |
| AI | per `ai_fallback_policy` (SEND_ORIGINAL / DROP / RETRY / QUARANTINE) |
| `empty_after_clean` | the only transform-internal drop (`pipeline.py:1467`) |
| Session time-limited | **defer**, enqueue for after the suspension (`pipeline.py:935-949`) |
| Invalid user regex | **fail closed** — DROP with `invalid_regex_*` (`services.py:138`) |
| Invalid replace pattern | skip the entry, text unchanged (`text_replace.py:391`) |
| Target rate limit | defer, never an attempt (`delivery.go:221`) |
| Fence unavailable | refuse to dispatch unprotected (`delivery.go:245`) |
| Branding stage-1 ambiguous | mark UNKNOWN for reconciliation, never blind re-post |
