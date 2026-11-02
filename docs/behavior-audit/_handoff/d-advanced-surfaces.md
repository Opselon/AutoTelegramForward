# Agent D — Advanced Transforms, Links, Delivery & Control Surfaces

Stage 4.0 discovery-only audit. Baseline master `35ee530`, branch
`audit/complete-business-logic-inventory`. Every claim below carries a
`file:line` citation into that baseline. Nothing outside this file was
modified; no production code was written; nothing was committed.

Two notation notes used throughout:

* **P0** = the Python ingest pipeline (`core/application/pipeline.py`,
  `_apply_transforms` at `pipeline.py:1401`). **G0** = the Go Worker
  delivery engine (`worker/internal/engine/delivery/delivery.go`,
  `Engine.Process` at `delivery.go:136`). The coordinator has already
  documented the split (COORDINATOR_VOCABULARY §0/§1); this file keeps the
  same names.
* `NOT_VERIFIED` marks anything I could not reach with a primary source.
  `STORED_BUT_UNUSED` marks a persisted setting no production read path
  consumes.

---

## 1. Inventory — AdvancedSettingsEngine (`ADV-*`)

Definition: `core/domain/services.py:1409-1567`. Class constants at
`services.py:1429-1440`:

| Key | Constant | Value |
|---|---|---|
| `crypto_mode` | `CRYPTO_ADDON_KEY` | `services.py:1429` |
| `watermark` | `WATERMARK_ADDON_KEY` | `services.py:1430` |
| `link_buttons` | `LINK_BUTTONS_ADDON_KEY` | `services.py:1431` |
| `affiliate_kit` | `AFFILIATE_ADDON_KEY` | `services.py:1432` |
| `ocr_image_ai` | `OCR_ADDON_KEY` | `services.py:1433` |
| `smart_ai_rewrite` | `SMART_REWRITE_ADDON_KEY` | `services.py:1434` |
| `keyword_reply` | `KEYWORD_REPLY_ADDON_KEY` | `services.py:1435` |
| `auto_delete_message` | `AUTO_DELETE_ADDON_KEY` | `services.py:1436` |

Bounds: `WATERMARK_MAX_LEN = 200`, `KEYWORD_REPLY_MAX_LEN = 4096`
(`services.py:1439-1440`).

| ID | Feature | Store | Authority read by engine | Status | Evidence |
|---|---|---|---|---|---|
| ADV-01 | Watermark appended to body | `metadata["watermark_text"]` (alias `metadata["watermark"]`) — `entities.py:1831-1841` | `AdvancedSettingsEngine.apply_content_transforms` step 1 | FULLY_IMPLEMENTED | `services.py:1458-1464`; pipeline caller `pipeline.py:1555` |
| ADV-02 | Paid add-on gate for watermark | — (watermark has **no** add-on gate) | none | NOT_VERIFIED (by design? see ADV-GAP-1) | `services.py:1458-1464` — no `is_addon_enabled` call, unlike steps 2 and 3 |
| ADV-03 | Link buttons rendered as text block | `metadata["link_buttons"]` — `entities.py:1602-1609` | `apply_content_transforms` step 2, gated `is_addon_enabled("link_buttons")` | FULLY_IMPLEMENTED | `services.py:1469-1476` |
| ADV-04 | Affiliate kit URL prefixing | `metadata["affiliate_kit"]` — `entities.py:1615-1622` | `apply_content_transforms` step 3, gated `is_addon_enabled("affiliate_kit")` | FULLY_IMPLEMENTED | `services.py:1480-1490` (`URL_REGEX.sub`, prefix prepended only when match starts with `http`) |
| ADV-05 | Keyword-reply add-on | `keyword_confirmation`/`keyword_reply` metadata + `paid_addons["keyword_reply"]` — `entities.py:1787-1809` | `classify_keyword_trigger` → pipeline enqueues `job_kind="keyword_reply"` | FULLY_IMPLEMENTED | `services.py:1495-1521`; `pipeline.py:1304-1340` (Agent C owns KW-*; D documents only the add-on gate) |
| ADV-06 | Auto-delete TTL | `metadata["auto_delete_seconds"]` + `paid_addons["auto_delete_message"]` — `entities.py:1812-1826` | `get_auto_delete_seconds`, bounded `max(1, min(ttl, 31_536_000))` | FULLY_IMPLEMENTED | `services.py:1524-1536`; payload write `durable_queue.py:369`; Go consume `delivery.go:1070` |
| ADV-07 | Edit-time window | `metadata["edit_time_limit_seconds"]` — `entities.py:1811-1824` | `is_edit_within_window` (0 = no window, missing dates never drop) | FULLY_IMPLEMENTED | `services.py:1539-1556`; caller `pipeline.py:824-833` |
| ADV-08 | Dispatch delay | `metadata["forward_delay_seconds"]` over legacy column `delay_seconds` — `entities.py:1759-1762` | `get_dispatch_delay` = `float(rule.forward_delay_seconds or 0.0)`, both clamped by `MAX_FORWARD_DELAY_SECONDS = 86400.0` (`entities.py:142`, `clamp_delay_seconds` `entities.py:154`) | FULLY_IMPLEMENTED | `services.py:1558-1567`; enqueue write `durable_queue.py:368+406`; Python direct path sleeps `dispatcher.py:518-520`; Go honors it via the claim gate (no double wait) — `durable_queue.py:1984-1993` |
| ADV-09 | Server-side entitlement check | `metadata["paid_addons"]` — `entities.py:1735-1742` | `is_addon_enabled` — only consulted from `services.py:1469/1481/1510/1533` | FULLY_IMPLEMENTED | `entities.py:1843-1856` |
| ADV-10 | Transform failure degradation | — | pipeline catches and delivers pre-transform text | FULLY_IMPLEMENTED | `pipeline.py:1554-1572` (exception → log + untransformed text, never drop) |

### ADV verified call paths (pasted)

Watermark/link-buttons/affiliate, durable path:

```
core application pipeline._apply_transforms          pipeline.py:1555
  → AdvancedSettingsEngine.apply_content_transforms  services.py:1442
      rule.watermark_text  entities.py:1831
      rule.link_buttons    entities.py:1602
      rule.affiliate_kit   entities.py:1615
      rule.is_addon_enabled entities.py:1843
```

Auto-delete, durable path (two independent executors):

```
(1) Python worker:
  DurableDeliveryQueue._process_job → _dispatch_to_telegram
    payload written at durable_queue.py:369 (get_auto_delete_seconds)
    consumed at        durable_queue.py:1332-1335
    executed by        _schedule_auto_delete  durable_queue.py:1824-1848
      asyncio.sleep(min(ttl, 31_536_000)) then client.delete_messages
(2) Go Worker:
  Engine.Process → scheduleAutoDelete   delivery.go:1066-1110
    j.Payload.AutoDeleteSeconds()        job/payload.go:199
    scheduler.Schedule(Action{Type: ActionAutoDelete}) scheduler/store.go:49
  Scheduler loop claims due actions:     scheduler/engine.go:129-199
    delete(): gateway delete              scheduler/engine.go:282-309
```

### ADV parity findings

**ADV-P1 — the TTL bound disagrees across the three write sites.**
* Setting registry: `auto_delete_seconds` maximum **604800** (7 days) —
  `core/domain/rule_settings.py:154-156`.
* Engine: bounded to **31_536_000** (1 year) — `services.py:1536`.
* Go API metadata normalizer: clamps `auto_delete_seconds` only for
  negativity, **no upper bound** — `api/internal/handlers/handlers.go:545-552`
  (the bounded list is `forward_delay_seconds`/`edit_time_limit_seconds`/
  `auto_delete_seconds`, but only `forward_delay_seconds` gets
  `maxDelaySeconds`).
* Fixture: `{"auto_delete_seconds": 90000000}` (≈2.85 y).
  REST `PUT /rules/{id}` with metadata → stored as-is (no clamp). Settings
  API `PATCH /rules/{id}/settings` → rejected/normalized to 604800. Engine
  read → 31_536_000. Three surfaces, three answers.
* Expected: one documented ceiling, enforced identically at every writer and
  the reader. Severity **medium** (behaviour differs by entry point, not by
  risk). Recommended separate fix PR: unify the bound in
  `rule_settings.SETTING_SPECS`, `AdvancedSettingsEngine` and
  `normalizeAdvancedMetadata`.

**ADV-P2 — watermark bypasses the add-on gate that its catalogue entry
implies.** `paid_addons["watermark"]` exists in every catalogue
(`web/frontend/src/addons.ts:78`, `mobile/lib/features/rules/
rule_editor_screen.dart:1662` via the paid section, pro_bot_ui
`_ADDON_ROWS` `pro_bot_ui.py:3269`), but `apply_content_transforms` step 1
never consults `is_addon_enabled("watermark")` (`services.py:1458-1464`).
Turning the catalogue switch off in any client leaves the watermark
appended. Severity **medium**. Fix PR: add the gate (or delete the
catalogue switch — the setting is `STORED_BUT_UNUSED` for gating purposes
today). Note `WATERMARK_ADDON_KEY = "watermark"` is **defined but never
read** (`services.py:1430` vs. the four keys read at 1469/1481/1510/1533).

**ADV-P3 — auto-delete add-on key name disagrees between engine and both
catalogues.** Engine reads `paid_addons["auto_delete_message"]`
(`services.py:1436+1533`); the web catalogue toggles `paid_addons["auto_del"]`
(`web/frontend/src/addons.ts:48-50`, written by
`web/frontend/src/components/TaskOverviewModal.tsx:564-570`); the Flutter
editor toggles `paid_addons["auto_del"]`
(`mobile/lib/features/rules/rule_editor_screen.dart:1662`); the bot's
`_ADDON_ROWS` toggles `auto_del` (`pro_bot_ui.py:3258`). Only the tests
(`core/tests/test_phase2_advanced_settings_execution.py:255`, which set
`auto_delete_message`) and the bot's `radon:{id}:auto_del` handler path
(`pro_bot_ui.py:2094` reads `addons.get("auto_del")`) line up with the
engine — and the bot's toggle writes whatever `_ADDON_ROWS` names.
Fixture: task with `paid_addons={"auto_del": true}`, `auto_delete_seconds:
60`. Every client UI reports auto-delete ON; `get_auto_delete_seconds`
returns **0** (`services.py:1531-1534`). Severity **high** — the feature is
silently dead in the default configuration path. Recommended separate fix
PR: rename the engine constant to `auto_del` (or the catalogues to
`auto_delete_message`) and add a fixture pinning one spelling.

**ADV-P4 — `forward_delay_seconds` clamp boundaries disagree.** Registry
maximum 86400 (`rule_settings.py:152-153`, matching
`MAX_FORWARD_DELAY_SECONDS`), Go API `maxDelaySeconds = 3600.0` for metadata
on rule create/update (`api/internal/handlers/handlers.go:529+546`). A
dashboard task saving a 2-hour delay through the REST metadata path is
clamped to 1 hour; the same value through the settings API is clamped to 24
hours; the entity itself clamps to 24 hours (`entities.py:154`). Severity
**medium**. Fix PR: make `api/internal/handlers/handlers.go` reuse the
registry maximum.

---

## 2. Header / Footer / Captions

### 2.1 Header/footer engines (two)

**(a) `FilterEngine.apply_header_footer`** — `core/domain/services.py:247-261`.
Joins `[header?, body?, footer?]` with `"\n\n"`, strips each part, and
prevents re-insertion when the body already starts with the header
(`services.py:252`) or ends with the footer (`services.py:259`). **This is
the engine P0 uses** (`pipeline.py:1546-1549`).

**(b) `HeaderFormatter.format_text`** — `core/domain/services.py:320-387`.
Placeholder interpolation; used **only** by `FilterEngine.transform_text`
(`services.py:307-309`), i.e. the legacy/edit-sync path — **not** by P0's
`_apply_transforms` (the pipeline calls `apply_header_footer` directly with
unformatted header/footer at `pipeline.py:1546-1549`, so `{date}`-style
placeholders are **not** interpolated in the durable path).

Placeholders supported (`services.py:358-379`), substituted in both
`{name}` and `[[name]]` spellings (`services.py:384-386`):

`rule_name`, `category`, `date`, `source_link`, `sender_name`, `chat_title`,
`source_chat`, `ORIGIN_TEXT`, `ORIGIN_NAME`, `ORIGIN_USERNAME`,
`ORIGIN_USERID`, `ORIGIN_POST_ID`, `ORIGIN_CHAT_ID`, `ORIGIN_POST_DATE`,
`ORIGIN_NAME_URL`, `SOURCE_NAME`, `SENDER_CHAT`, `FORWARD_FROM_CHAT`,
`FROM_USER`. Plus caller-supplied `custom_vars` (`services.py:380-381`).

**HDR-P1 (parity, medium):** the durable pipeline does not format
placeholders while the legacy direct path and `TestRule` do
(`servicers.py:1521-1524`). A task whose header contains `{date}` renders
the literal `{date}` when delivered through the durable queue and the
rendered date when delivered by `sync_engine.py:261` or previewed via
`POST /api/simulate`. Fixture: header `"📰 [[ORIGIN_NAME]] {date}"` → P0
output `"📰 [[ORIGIN_NAME]] {date}"`; preview output `"📰 MyChannel
2026-11-01 00:00 UTC"`. Recommended separate fix: format placeholders in P0
before `apply_header_footer`, or document the durable path as
non-templated.

### 2.2 Header/footer storage (three keys, deliberately reconciled)

`rule.header` prefers `custom_header`, falling back to `metadata["header"]`
(`entities.py:589-593`); the setter writes **both** (`entities.py:595-600`).
`clear_header()` exists specifically because a third key
(`metadata["custom_header"]`) used to survive a delete — it now clears all
three (`entities.py:603-620`, docstring at 605-615). Footer mirrors
(`entities.py:622-630`). `header_text`/`footer_text` are read as aliases at
`pipeline.py:1546-1547`.

### 2.3 `CaptionManagementEngine` — `core/domain/services.py:1118-1185`

`CAPTION_MAX_LIMIT = 1024`, `TEXT_MAX_LIMIT = 4096` (`services.py:1123-1124`).
`format_caption` (`services.py:1127`):

1. header prepend with dedup, shift entity offsets by the UTF-16 length of
   the header (`services.py:1146-1160`);
2. footer append with dedup (`services.py:1152-1153`);
3. overflow: `split_overflow=True` → cut at the last newline before
   `CAPTION_MAX_LIMIT-30`, else last space, else a hard cut 20 chars short
   (`services.py:1167-1171`), returning `(truncated, valid_entities,
   overflow_text)`; entities beyond the cut are dropped
   (`services.py:1177-1181`).

**Production caller is exactly one: the branding hop**
(`core/application/branding_hop.py:192-196`, with
`split_overflow=rule.split_long_caption`). The engine is **not** called by
the durable dispatch path or P0; `caption_max_length`
(`entities.py:251`) is read only by backup/gRPC round-trip (`servicers.py`
241/493/1066/1211) and never by any transform — `STORED_BUT_UNUSED` as a
transform authority.

Careful: `entities.py:408` `split_long_caption` is a **first-class field**
that *is* read (`branding_hop.py:195`), while `caption_max_length` is the
unused sibling.

`TelegramMessageCapabilityMatrix` (`services.py:1188+`) is a static
capability table (receive/detect/copy/forward/caption/formatting/
intermediate_hop per media type) with no production reader found — see
Unknowns.

---

## 3. LinkManagementEngine (`LNK-*`)

Definition `core/domain/services.py:758-1116`. Eight policy values in the
`LinkPolicy` enum (`core/domain/value_objects.py:151-159`):
`PRESERVE_ALL`, `REMOVE_ALL_URLS`, `REMOVE_EXTERNAL_URLS`,
`REMOVE_SELECTED_DOMAINS`, `ALLOWLIST_ONLY`, `REWRITE_LINKS`,
`REMOVE_BUTTON_URLS`, `CUSTOM_LINK_POLICY`.

| ID | Feature | Store | Authority | Status | Evidence |
|---|---|---|---|---|---|
| LNK-01 | Policy resolution (fail-closed) | `forward_rules.link_policy` col, default `PRESERVE_ALL` (`entities.py:403`; migrations `database.py:389`; SQLite `sqlite_repositories.py:403`, Postgres `postgres_repositories.py:393`) | `resolve_policy` — unknown value → PRESERVE_ALL + one log line | FULLY_IMPLEMENTED | `services.py:1001-1027`; warn-once set `_warned_policies` `services.py:766` |
| LNK-02 | `remove_links` flag upgrades policy | first-class `remove_links` (`entities.py:402`) | `resolve_policy(..., remove_links=True)` maps PRESERVE_ALL → REMOVE_ALL_URLS only | FULLY_IMPLEMENTED | `services.py:1023-1026`; gate `pipeline.py:1438` |
| LNK-03 | Domain allowlist | `forward_rules.domain_allowlist` (`entities.py:404`) | `_should_remove` ALLOWLIST_ONLY / CUSTOM | FULLY_IMPLEMENTED | `services.py:868-874` |
| LNK-04 | Domain blocklist | `forward_rules.domain_blocklist` (`entities.py:405`) | `_should_remove` REMOVE_SELECTED_DOMAINS / CUSTOM | FULLY_IMPLEMENTED | `services.py:866-867`, `871-872` |
| LNK-05 | Domain rewrite map | `forward_rules.link_rewrite_map` (`entities.py:406`) | `_rewrite_url` subdomain-aware | FULLY_IMPLEMENTED | `services.py:877-883` (match `dom == old` or `dom.endswith("."+old)`) |
| LNK-06 | Entity-aware removal (text_link) | runtime payload | text_link entities stripped of URL metadata, anchor text kept | FULLY_IMPLEMENTED | `services.py:887-903` |
| LNK-07 | UTF-16 offset shifting | runtime payload | removal shift/shrink + post-normalization `_shifted` remap | FULLY_IMPLEMENTED | `services.py:920-947`, `962-998` |
| LNK-08 | SSRF guard | pure function | `is_safe_url`: scheme allowlist http/https/tg, private/loopback/link-local/multicast/reserved/unspecified IPs, `localhost`, `metadata.google.internal` | FULLY_IMPLEMENTED | `services.py:769-804` |
| LNK-09 | Domain extraction / matching | pure functions | `extract_domain` (strips `www.`, lowercases) `services.py:807-819`; `matches_domain` (equality or subdomain `endswith("."+item)`) `services.py:822-833` | FULLY_IMPLEMENTED | — |
| LNK-10 | `apply_to_payload` (pipeline entry) | runtime | entity source = `payload.caption_entities` when `has_media` else `payload.entities` | FULLY_IMPLEMENTED | `services.py:1040-1062`; caller `pipeline.py:1439-1452` |
| LNK-11 | Inline-keyboard filtering | runtime reply_markup | `filter_inline_keyboard` per policy | **UNREFERENCED_HELPER** | defined `services.py:1065-1115`; **zero production callers** (see LNK-U1) |
| LNK-12 | Mentions exempt from link removal | runtime | URL Regex `URL_REGEX` (`services.py:26-31`) matches `@username`, engine skips matches starting with `@` | FULLY_IMPLEMENTED | `services.py:911-917` |

### The nine link authorities (brief §9 list, one entry each)

The brief asks for the distinct features that all sound like "links". Each
reads a different store; none is a duplicate of another:

1. **`remove_links`** — first-class bool (`entities.py:402`). Read by LME
   `resolve_policy` (`services.py:1051`) and by ContentPolicy
   (`content_policy.py:703/737/914-920`, which strips via
   `FilterEngine.remove_urls`). Two engines, one flag.
2. **`link_replacement`** — `metadata["link_replacement"]`
   (`entities.py:1856`). Read by `FilterEngine.replace_links`
   (`services.py:225-232`) **only on the elif branch** of P0
   (`pipeline.py:1455-1457`) — i.e. only when `link_policy` is
   PRESERVE_ALL *and* `remove_links` is false.
3. **`link_policy`** — column (`entities.py:403`). LME authority
   (`services.py:1049-1052`).
4. **`domain_allowlist`** — column (`entities.py:404`), LNK-03.
5. **`domain_blocklist`** — column (`entities.py:405`), LNK-04.
6. **`link_rewrite_map`** — column (`entities.py:406`), LNK-05.
7. **`blocked_media_types` containing `link`** — media-side gate; the
   media filter reads `allowed/blocked media types`
   (`core/domain/media_filter.py:77/94` per the coordinator). Agent A owns
   `MED-*`; documented here only as a distinct link entry point.
8. **`FilterRule.block_links`** — attached filter object, evaluated at
   `pipeline.py:918` and re-evaluated in Go at `delivery.go:212-224`
   (comment at `delivery.go:193-199`). Agent A/B territory (`FLT-*`); the
   Go re-evaluation is the delivery-side authority D owns.
9. **ContentPolicy link logic** — `block_links` (drop) + `remove_links`
   (strip) inside `metadata["content_policy"]`
   (`content_policy.py:702-703`, `840`, `914-920`). Agent A owns `POL-*`;
   the interaction with LME at `pipeline.py:1496-1514` is documented in
   §3.1 below.

Additionally, the catalogue-level `TG_DOMAINS = {"t.me", "telegram.me",
"telegram.dog"}` (`services.py:763`) defines "external" for
`REMOVE_EXTERNAL_URLS` in both the text pass (`services.py:865`) and the
keyboard pass (`services.py:1094`).

### LNK verified call paths (pasted)

```
core application pipeline._apply_transforms            pipeline.py:1437
  gate: link_policy != "PRESERVE_ALL" OR remove_links  pipeline.py:1438
  → LinkManagementEngine.apply_to_payload              services.py:1030
      payload.caption_entities if has_media else payload.entities
                                                        services.py:1047
      resolve_policy(rule.link_policy, remove_links=...) services.py:1049
      → process_text_and_entities                      services.py:836
          _should_remove  services.py:860
          _rewrite_url    services.py:877
          URL_REGEX       services.py:26
          utf16_len / char_to_utf16_pos  (core/domain/unicode.py, used at
                                          services.py:920-921, 978)
          _shifted        services.py:973
  results written back:
      payload.caption_entities / payload.entities      pipeline.py:1449-1452
      evaluation.links_removed = True                  pipeline.py:1453-1454
  failure mode: LME exceptions propagate — the only wrapping try/except is
      the cleaner's (pipeline.py:1412-1434), NOT the LME call.
```

### LNK findings

**LNK-U1 — `filter_inline_keyboard` is dead code (UNREFERENCED_HELPER).**
`grep -rn filter_inline_keyboard` over the whole repo returns exactly its
definition (`services.py:1065`) and no callers. The only other
inline-keyboard manipulation on the send path is the cleaner's
`strip_buttons`, which nulls the whole markup
(`pipeline.py:1420-1427`, on `cleaner.strip_buttons`). Consequence: a task
with `link_policy=REMOVE_ALL_URLS` has the URL **text** stripped but its
inline **buttons** forwarded untouched. Fixture: message with text
`"see https://evil.example"` and one `InlineKeyboardButton(url=
"https://evil.example")`; engine output text = `"see"`, markup unchanged.
Severity **medium** (policy intent not honoured on the markup surface).
Recommended separate fix PR: call `filter_inline_keyboard` from the
dispatch/branding path where the markup is attached.

**LNK-U2 — `link_policy`/`domain_*`/`link_rewrite_map` have no settings-
registry entries.** `SETTING_SPECS` (`core/domain/rule_settings.py:96`)
registers `remove_links` (265) but **not** `link_policy`,
`domain_allowlist`, `domain_blocklist`, or `link_rewrite_map`
(`grep link_policy core/domain/rule_settings.py` → no hit). So the
settings API (`PATCH /rules/{id}/settings`) cannot write them, while
`UpdateRule`/`enrichCustomMetadata` parity list
(`api/internal/handlers/handlers.go:465-481`) also omits all four. They are
writable only through the gRPC `UpdateRule` field mapping
(`servicers.py:64/72/272-274`), backup restore, and the raw
`custom_metadata_json` escape hatch. The web types *declare*
`link_policy`/`link_rewrite_map` (`web/frontend/src/types.ts:29/69/157/
171`) but no UI writes them. Severity **medium** (feature unreachable from
the documented settings surface). See §6.2 REST parity keys.

**LNK-P1 — Python vs Go link removal diverges on every axis.**
Fixture: `text = "Buy https://example.com/a now"` with a task
`link_policy=REMOVE_ALL_URLS`, `domain_blocklist=["example.com"]`,
`link_rewrite_map={"example.com": "aff.example.com"}`, plus a
`text_link` entity and an inline URL button.

* **P0 output**: `"Buy now"` — `URL_REGEX` spans
  `https?://[^\s]+|www\.[^\s]+|t(?:elegram)?\.(?:me|dog)/[^\s]+|@user`,
  offsets remapped, entity list clipped to the new length
  (`services.py:906-998`), trailing whitespace collapsed
  (`services.py:967-971`).
* **G0 output**: unchanged. The Go worker applies **no** link policy at
  all: `transport.ApplyTargetOverride` handles only
  `RemoveLinks`/`StripWords` from a **target-scoped** override
  (`worker/internal/engine/transform/transform.go:42-82`), and nothing
  constructs a `TargetOverride` in production — `grep ApplyTargetOverride`
  returns only the definition; `Engine.transforms` is created at
  `delivery.go:120` and never called. There is no `LinkPolicy`,
  `DomainAllowlist` or `link_policy` field anywhere in
  `worker/internal/domain/rule/rule.go` (fields listed `rule.go:14-54`).
* **Why this is correct today (documented, not a defect):** P0 bakes the
  *result* into the job payload (`durable_queue.py:345` `"text": text`);
  the Go worker's own header comment states the pipeline-side transforms
  "have ALREADY run on the ingest side — their result is the `text` in the
  job payload" (`transform.go:5-14`).
* **Where it becomes a real defect:** a job enqueued *before* a rule edit,
  or a rule whose `link_policy` changes while the job sits in the queue, is
  delivered with the old text; and any delivery path that re-reads the rule
  and re-derives text (none found — see DLV-P4) would diverge. Severity
  **informational** (documented design), **upgraded to medium** the day a
  worker-side re-transform lands without porting `resolve_policy` +
  `process_text_and_entities`.
* Recommended separate fix PR (only if worker-side transforms are
  introduced): port `LinkPolicy` + the domain lists + rewrite map to
  `worker/internal/domain/rule`, and replace the `urlRegex` blank
  substitution in `ApplyTargetOverride` with the trimmed variant (Go
  `strings.ReplaceAll(text, url, "")` leaves the surrounding spaces, while
  P0 collapses them — `services.py:968`).

**LNK-P2 — Go `urlRegex` and Python `URL_REGEX` are byte-identical, but
the *predicate* is not.** Go `HasLink`
(`worker/internal/engine/transform/transform.go:116-124`) adds substring
probes for `http://`, `https://`, `t.me/` that Python's regex does not
need; the Go filter's `block_links` gate additionally lowercases
(`worker/internal/engine/filter/filter.go:210-216`). An `https://` in a
non-ASCII-cased context matches identically, but a bare `t.me/` in a
message that the Python regex rejected as not-a-link (e.g.
`\nt.me/x` at a boundary) can answer differently. Severity **low**; both
engines agree on every realistic fixture. Fixture to pin:
`"chat t.me/abc def"` → Python `URL_REGEX` matches (t.me alternative has
no lookbehind) — both report a link. NOT a divergence today; recorded so
the fixture exists.

**LNK-P3 — `remove_links` double-strips on the durable path, but the mention
preservation survives (verified; not a defect).** With
`remove_links=true` and no `link_policy`, P0 runs the LME (REMOVE_ALL_URLS,
`pipeline.py:1438-1448`) and then, unconditionally, the ContentPolicy
transform, whose `remove_links` flag was folded in from the same
first-class field by `entities.py` (`content_policy` property at
`entities.py:1359-1378`, `remove_links` fold at 1371-1373) and runs
`FilterEngine.remove_urls` again (`content_policy.py:913-920`,
`remove_urls` at `services.py:158-170`). Both passes are URL-only and
idempotent on URLs; the ContentPolicy path uses `remove_urls` (markdown +
`https?://`/`www.`/`t.me` patterns, `services.py:161-163`), **not**
`remove_links`, so `@usernames` survive both passes — verified by direct
execution: `FilterEngine().remove_urls("buy https://x.com @seller")` →
`"buy @seller"`; `FilterEngine().remove_links(...)` (the URL_REGEX variant
that also strips mentions, `services.py:171-176`) → `"buy"`. The
mention-stripping variant `remove_links` is **not** on the durable path
(only the legacy `FilterEngine.transform_text` step 1 at `services.py:269-272`,
whose pipeline elif at `pipeline.py:1455-1457` is not even entered when the
LME ran). Severity **informational**: the double strip is wasted work, not
a behavior change, and the LME's documented mention preservation
(`services.py:911-917`) holds on the durable path. No fix PR recommended;
a fixture should pin it so a future refactor of `ContentPolicy.transform`
(e.g. swapping `remove_urls` for `remove_links`) cannot silently start
dropping `@mentions`. The pipeline comment at `pipeline.py:1492-1495`
(policy runs *after* the link engine so "the policy sees the text that will
actually be sent") covers duplicate work, not mention stripping.

---

## 4. AI transforms (`AI-*`)

| ID | Feature | Store | Authority | Status | Evidence |
|---|---|---|---|---|---|
| AI-01 | Rewrite execution | runtime | `_run_ai_transform` — `pipeline.py:1616-1722` | FULLY_IMPLEMENTED | gate `pipeline.py:1520-1531` |
| AI-02 | Config resolution (owner-scoped) | `ai_configs` table (`AIConfig` `entities.py:2413-2430`) | `AITransformer._resolve_config` refuses foreign/ownerless configs with a distinct reason | FULLY_IMPLEMENTED | `core/infrastructure/ai/transformer.py:112-135` |
| AI-03 | Active gate (single authority) | `ai_config_id` + `paid_addons["ai_rewrite"]` | `rule.ai_rewrite_active` — no config → False; `paid_addons["ai_rewrite"]` present → it decides; else True | FULLY_IMPLEMENTED | `entities.py:1679-1699` |
| AI-04 | Fallback policy | first-class `ai_fallback_policy` (`entities.py:426`); enum `AIFallbackPolicy` `value_objects.py:170-174` | `_handle_ai_fallback` | FULLY_IMPLEMENTED | `pipeline.py:1724-1801` |
| AI-05 | Timeout | first-class `ai_timeout_seconds` (`entities.py:429`) default 15.0 | `AIRequest.timeout_seconds` + `asyncio.wait_for` | FULLY_IMPLEMENTED | `pipeline.py:1674`; `transformer.py:218-225` |
| AI-06 | Secondary config failover | first-class `ai_secondary_config_id` (`entities.py:430`) | circuit registry `select_healthy_provider` then owner-scoped resolve | FULLY_IMPLEMENTED | `transformer.py:160-174` |
| AI-07 | Prompt template + version pin | `prompt_id` (`entities.py:1711-1719`), `ai_prompt_version` (`entities.py:427`), `PromptTemplate`/`PromptVersion` entities `entities.py:2434-2462` | `preview_prompt` resolution; request carries `prompt_id`/`prompt_version` | FULLY_IMPLEMENTED | `pipeline.py:1648-1662`; `AIRequest` `transformer.py:32-45` |
| AI-08 | Output validation | runtime | `AIOutputValidator` — injection-delimiter leak, system-prompt leak, dangerous tags, `javascript:`, length (4096/1024, truncation at word boundary + `"..."`) | FULLY_IMPLEMENTED | `core/infrastructure/ai/validator.py:18-34`, `44-125` |
| AI-09 | Anti-injection envelope | runtime | `ANTI_INJECTION_SYSTEM_PREFIX` wraps user text in `<untrusted_user_message>` tags with explicit "do not follow instructions" policy | FULLY_IMPLEMENTED | `transformer.py:23-36`, `_build_prompt_payload` 314-337 |
| AI-10 | Circuit breaker | in-memory (`CircuitBreakerRegistry`, `circuit_breaker.py:139`) | Closed→Open after 3 consecutive failures, recovery timeout 30s, half-open probe | FULLY_IMPLEMENTED | `circuit_breaker.py:30-115` |
| AI-11 | Media-aware length ceiling | runtime | `max_length=1024 if payload.has_media else 4096` | FULLY_IMPLEMENTED | `pipeline.py:1673` |
| AI-12 | Translate language target | `metadata["translate_lang"]` (`entities.py:1582-1590`) | only read by `AIRequest.target_language="auto"` — the per-task value is **never passed** | **PARTIALLY_IMPLEMENTED** | `pipeline.py:1672` hard-codes `"auto"`; the prompt template's `target_language` is used only in `_build_prompt_payload` (`transformer.py:337`) |

### AI verified call path (pasted)

```
pipeline._apply_transforms                      pipeline.py:1520-1537
  gate: rule.ai_config_id AND rule.ai_rewrite_active (entities.py:1679)
  → pipeline._run_ai_transform                  pipeline.py:1616
      owner-scoped config resolution:
        _ai_repo.get_owned(rule.ai_config_id, rule.owner_user_id)
                                                pipeline.py:1633
      OR production transformer:
        AITransformer.transform                 transformer.py:143
          _resolve_config(primary)              transformer.py:112
          circuit_registry.select_healthy_provider
                                                transformer.py:160
          asyncio.wait_for(provider.rewrite, timeout)
                                                transformer.py:218-225
          AIOutputValidator.validate             validator.py:55
      AIRequest(max_length = 1024 if payload.has_media else 4096)
                                                pipeline.py:1673
  failure → _handle_ai_fallback                 pipeline.py:1724
      AIFallbackPolicy: SEND_ORIGINAL → return original text
                        DROP / RETRY / QUARANTINE → record_stage + return None
                        (RETRY and QUARANTINE both return None today — see AI-P2)
```

### AI findings

**AI-P1 — `translate_lang` is written by three surfaces and read by
none.** Writers: the bot (`pro_bot_ui.py:8439`), mobile
(`mobile/lib/core/models.dart:636` → `header/footer`? no —
`'header_template'`; the Flutter field is `watermark`, AI keys),
the settings registry (`rule_settings.py:180-182` registers `translate_lang`
with help text "Target language for auto-translation ('' = off)"), and gRPC
(`_coerce_str` at `servicers.py` v17 str list — not present; the v17 list
contains `topic_id` etc., `translate_lang` is absent there but present in
the Go parity list? — `handlers.go:465-481` does **not** list
`translate_lang`). Reader: `pipeline.py:1672` sets
`target_language="auto"` unconditionally; no other production code reads
`rule.translate_lang` (`grep translate_lang core/ | grep -v test | grep -v
entities/rule_settings/servicers` → only `pro_bot_ui.py:3125/4045` for
*display*). The bot's UI toggles it (`pro_bot_ui.py:8439`), the settings
schema documents it, and it has zero effect on the prompt. **Status:
STORED_BUT_UNUSED.** Severity **medium** (a documented setting with no
behaviour). Recommended separate fix: pass
`getattr(rule, "translate_lang", "") or "auto"` into `AIRequest` at
`pipeline.py:1672`.

**AI-P2 — `RETRY` and `QUARANTINE` are the same behaviour.**
`_handle_ai_fallback` (`pipeline.py:1768-1799`) records a stage failure and
returns `None` for both; RETRY performs no retry and QUARANTINE writes no
quarantine row. Fixture: `ai_fallback_policy=RETRY` with a failing provider
→ message dropped, exactly as `DROP` (metrics differ:
`ai_fallback_retries` vs `ai_fallback_drops`, `pipeline.py:1769` vs
`1752`). Severity **medium**. Recommended separate fix: implement the
retry/quarantine or drop the enum values to two.

**AI-P3 — Go preview surfaces call the same transformer (verified one).**
`TestAIRewrite`/`TestPrompt` servicers (`servicers.py:2443-2468`,
`2589-2626`) route through `ai_transformer.transform`, so preview and
production share the transformer; the *gate* differs — previews do not
check `ai_rewrite_active` (by design, they test a config). No defect; the
parity note is that `TestRule`'s rendered text comes from
`TransformationService.transform_text` (the **legacy** pipeline,
`servicers.py:1521-1524`), so the simulator's `rendered_text` shows
link_replacement/placeholder-formatted output while the durable path shows
LME/`apply_header_footer` output — the same divergence as HDR-P1. Fixture
to pin both: task with `link_replacement="(link)"`, header `"{date}"`,
media=false, sample `"a https://b.io c"`.

**AI-P4 — `ai_creativity` / `ai_max_tokens` are display-only.** Registered
(`rule_settings.py:191-204`), surfaced by the bot
(`pro_bot_ui.py:3170-3171`), editable in mobile
(`mobile/lib/core/models.dart:617-618`), and **never read** by the
pipeline, `AIRequest`, `AITransformer`, or any provider: the provider calls
use `config.temperature` (`providers.py:161/211/239`) which comes from the
`AIConfig` row, not the per-task values. **STORED_BUT_UNUSED** as a
behaviour. Severity **low**. `ocr_image_ai`, `news_digest`, `crypto_mode`
are the same class (see §7).

### AI fixtures proposed

* `ai-fallback-retry-drops-like-drop` — RETRY policy + provider failure →
  message dropped (pins AI-P2).
* `ai-translate-lang-ignored` — `translate_lang="fa"` + AI config →
  `AIRequest.target_language == "auto"` (pins AI-P1).
* `ai-mention-preserved-vs-contentpolicy` — covered by LNK-P3 fixture.

---

## 5. Delivery & transport (`DLV-*`)

### 5.1 State machine

`DeliveryStateMachine` (`core/domain/services.py:1337-1406`), statuses in
`core/domain/value_objects.py:94-110` (17 values). Allowed transitions
table at `services.py:1343-1380`; `resume_after_crash`
(`services.py:1389-1406`) resumes a job that already has
`intermediate_message_id` at `FORWARDING_TO_B` instead of re-copying to C.
**Reader caveat:** `ALLOWED_TRANSITIONS` has no production caller I could
find (the queue drives transitions directly) — `UNREFERENCED_HELPER` as an
authority, though the *statuses* themselves are the queue's status set.

### 5.2 Queue and job payload

`core/infrastructure/telegram/durable_queue.py`:

* `enqueue_delivery` — `durable_queue.py:273-443`. Backpressure guard
  (`273-290`, pending ≥ `_max_queue_size` → refuse + audit), payload dict
  built at `323-381`, atomic mapping+job write `_enqueue_atomic`
  (`411-435`, one transaction; INSERT OR IGNORE makes replay idempotent).
* Job payload keys (what is "baked in"): `session_id`, `source_chat_id`,
  `source_message_id`, `target_chat_id`, `text` (**the already-transformed
  text**), `effective_text`, `has_media`, `media_type`, `media_group_id`,
  `album_message_ids`, `album_mode`, `delivery_transport`, `bot_token_id`,
  `inline_buttons`, `forward_mode`, `content_mode`, `fallback_enabled`,
  `owner_user_id`, `delay_seconds`, `advanced_delay_seconds`,
  `auto_delete_seconds`, `topic_id`, `debug_mode`, `process_raw_text`,
  `button_to_text`, `is_reply`, `correlation_id`, `branding`
  (`durable_queue.py:328-381`). **No `link_policy`, no domain lists, no
  AI config, no header/footer** — the Go worker physically cannot re-run
  those transforms (matches LNK-P1's "by design" reading).
* Claim gate: the delay is written into `next_retry_at` once, from
  `AdvancedSettingsEngine.get_dispatch_delay` (`durable_queue.py:404-408`),
  so the worker's claim query `AND next_retry_at <= ?`
  (`worker/internal/queue/sqlite.go:221`, `postgres.go:218`) enforces it
  without anyone sleeping.

### 5.3 Python worker (delivery engine in-process)

`DurableDeliveryQueue._process_job` → `_dispatch_to_telegram`
(`durable_queue.py:1957`). Notable gates, in order:

1. account time-limit check (`durable_queue.py:1075-1085`);
2. rule exists / is active / is auto-paused / is paused
   (`durable_queue.py:1095-1140`) — a deleted or disabled rule **fails**
   the job, a paused rule **defers** it;
3. transport selection: `payload["delivery_transport"] == "BOT"` →
   `_bot_route` (`durable_queue.py:1138-1146`), which resolves the token
   **owner-scoped** (`durable_queue.py:1849-1878`) and refuses (defer 15s,
   no silent USER fallback) when the token is unusable
   (`durable_queue.py:1141-1151`);
4. send-side idempotency fence — `DeliveryAttemptKey`
   (`value_objects.py:113-148`) → `claim_or_check`
   (`durable_queue.py:1258-1310`); `already_sent` → mark SENT with the
   recorded id; `in_flight`/`stale`/`unknown` → defer for reconciliation;
5. dispatch: `forward_mode`/`content_mode`/album handling
   (`durable_queue.py:1964-2268`), topic routing
   (`durable_queue.py:2008-2018`), `is_reply` forces TEXT_ONLY copy
   (`durable_queue.py:1980-1984`);
6. auto-delete scheduling (`durable_queue.py:1332-1335`, executor
   `1824-1848`).

### 5.4 Go Worker

`Engine.Process` — `worker/internal/engine/delivery/delivery.go:136-350`:

1. **preflight** (comment `136-163`: everything that can decide "do not
   send" runs before the fence is claimed): rule-store outage → defer
   without an attempt (`169-176`); rule missing/disabled/auto-paused →
   `MarkFailed` (`178-204`); live pause → defer for the remaining window
   (`206-229`);
2. attached filter re-evaluation — `e.filters.Evaluate(text, ...)`; Drop →
   `MarkFailed` (`delivery.go:212-224`), with the explicit comment that
   ingest-side content policies are "baked into the job payload"
   (`delivery.go:193-199`);
3. per-target rate limit `targetRateWait` (`delivery.go:822-845`, in-memory
   per-process cooperation window, 0 = unlimited);
4. send-side fence: `FenceClaim` (`delivery.go:231-269`) with
   `FenceAlreadySent` → SENT without re-send, `FenceInFlight/Stale/
   Unknown` → defer 15s for reconciliation;
5. branding stage 1 — `ensureBrandingStage1` (`delivery.go:965-1041`):
   intent plans (`hopPlan.isIntent()`, `branding_stage.go:141`) only;
   intermediate-message checkpoint reuse (`delivery.go:986-995`);
   `BrandingStage1` RPC with ambiguous-outcome → `MarkUnknown`
   (`delivery.go:1012-1020`); then `updateJobForStage2` rewrites the
   payload's `branding` blob (`delivery.go:1041-1065`);
6. `dispatch` (`delivery.go:351-530`): decision order documented at
   `353-368` — branding hop → DIRECT_FORWARD (bot route has no native
   forward, falls through to copy with a log, `delivery.go:410-415`) →
   album → content modes → plain text; each restricted-source failure
   falls back to copy/re-upload only when `payload.FallbackEnabled()`;
7. `FenceRecordSent` **before** the job-row commit
   (`delivery.go:323-330`, comment at 318-322);
8. `scheduleAutoDelete` (`delivery.go:1066-1110`);
9. failure handling `handleFailure` (`delivery.go:727-803`) with
   `backoffFor` capped at 5 min (`805-815`).

Transport selection `transportFor` (`branding_stage.go:31-44`) reads the
payload first, the live rule as fallback; `botTarget`
(`branding_stage.go:45-60`) builds the tenant-scoped token reference — the
raw token never enters the worker. Bot-unsupported operations fall back to
USER only when the typed refusal is `BOT_METHOD_INVALID` or
`BOT_TRANSPORT_UNAVAILABLE` (`unsupportedFailure`,
`branding_stage.go:68-83`, used at `delivery.go:385-393`, `425-435`,
`449-461`).

### 5.5 Scheduler (durable auto-delete)

`worker/internal/engine/scheduler/` — `store.go` (action/state/transport,
`ActionAutoDelete` at `store.go:49`; states PENDING/CLAIMED/FAILED,
`store.go:64-72`), `engine.go` (one bounded loop, one bounded pool;
`PollInterval` 1s, `BatchSize` 32, `MaxConcurrent` 4, `RPCTimeout` 30s,
`MaxBackoff` 10 min; `Defaults()` `engine.go:78-91`). Comment block at
`store.go:1-31` states the design contract explicitly: restart-safe (the
schedule is a table row), single-execution via atomic claim (PostgreSQL
`FOR UPDATE SKIP LOCKED` / SQLite single-writer), idempotent deletion,
**no payload storage** — the row carries identifiers only.

| ID | Feature | Authority | Status | Evidence |
|---|---|---|---|---|
| DLV-01 | Durable enqueue (atomic job+mapping) | Python `durable_queue.py:411-435` | FULLY_IMPLEMENTED | INSERT OR IGNORE replay idempotency, comment at 416-419 |
| DLV-02 | Send-side idempotency fence | `DeliveryAttemptKey` `value_objects.py:113-148`; Python `durable_queue.py:1258-1310`; Go `delivery.go:231-269` | FULLY_IMPLEMENTED | both paths keyed identically (`delivery.go:213-229` vs `durable_queue.py:1217-1240`) |
| DLV-03 | USER/BOT transport selection | `entities.py:851-870` (BOT only when a token is attached); Python `durable_queue.py:1138-1146`; Go `branding_stage.go:31-60` | FULLY_IMPLEMENTED | — |
| DLV-04 | Bot token resolution (owner-scoped, never in worker) | `durable_queue.py:1849-1878`; `branding_stage.go:45-60` | FULLY_IMPLEMENTED | — |
| DLV-05 | Bot unsupported-op fallback policy | `branding_stage.go:68-83` + `delivery.go:385/425/449` | FULLY_IMPLEMENTED | falls back only on typed refusal |
| DLV-06 | Per-target rate limit | Python `durable_queue.py:2285-2310` + `dispatcher.py:759-766`; Go `delivery.go:822-855` | FULLY_IMPLEMENTED | Go: in-memory **cooperation** guard on top of the durable lane (`delivery.go:816-821`) |
| DLV-07 | Per-task rate limit (`rate_limit_per_minute`) | column `entities.py:241` | **STORED_BUT_UNUSED** | see DLV-U1 |
| DLV-08 | Rate burst (`rate_limit_burst`) | column `entities.py:242` | **STORED_BUT_UNUSED** | see DLV-U1 |
| DLV-09 | Dispatch delay (durable) | claim gate `durable_queue.py:404-408`, claim SQL `sqlite.go:221` | FULLY_IMPLEMENTED | — |
| DLV-10 | Dispatch delay (direct path) | `dispatcher.py:518-520` sleeps `min(delay, MAX_FORWARD_DELAY_SECONDS)` | FULLY_IMPLEMENTED | deprecated but still reachable |
| DLV-11 | Auto-delete (Python) | `durable_queue.py:1824-1848` | PARTIALLY_IMPLEMENTED | in-memory `asyncio.sleep` — lost on restart (see DLV-P1) |
| DLV-12 | Auto-delete (Go scheduler) | `delivery.go:1066-1110` + scheduler package | FULLY_IMPLEMENTED | durable, bounded |
| DLV-13 | Branding hop execution (worker-owned) | `delivery.go:965-1065`, `branding_stage.go` | FULLY_IMPLEMENTED | intent plan + checkpoint reuse |
| DLV-14 | Restricted-source fallback opt-out | `payload_data["fallback_enabled"]` (`durable_queue.py:356`); consumers `delivery.go:411/434/485-530`, `durable_queue.py` copy paths | FULLY_IMPLEMENTED | — |
| DLV-15 | Keyword-reply deliverable isolation | `job_kind` on the id + table UNIQUE + `payload["is_reply"]` forcing TEXT_ONLY | FULLY_IMPLEMENTED | `durable_queue.py:290-297+421-430`, `delivery.go:367-384`, `worker/internal/queue/sqlite.go:476` |

### DLV findings

**DLV-U1 — `rate_limit_per_minute` and `rate_limit_burst` are persisted
and round-tripped but never read by any limiter.** Columns exist
(`entities.py:241-242`; migrations `database.py:356-357`; SQLite mapping
`sqlite_repositories.py:365`; Postgres `postgres_repositories.py:381-382`;
backup `backup_service.py:199/599`; gRPC `servicers.py:234-235/241`).
Actual limiters read **other** sources:
`DurableDeliveryQueue._max_per_minute` comes from the constructor
(`durable_queue.py:120`), which `core/main.py:379` fills from
`cfg.queue_max_per_minute_per_target` (`core/config.py:99`, env
`ATF_QUEUE_MAX_PER_MINUTE`, `config.py:177`) — a **global**, not per-task;
the legacy dispatcher uses `set_rate_limit` (`dispatcher.py:230-232`)
which has **no production caller** (`grep set_rate_limit` → definition
only); the Go worker uses `MaxPerMinutePerTarget` from env
(`worker/internal/config/config.go:77`, `worker/cmd/worker/main.go:162`)
— also global. `rate_limit_burst` has no reader anywhere. Fixture: task
with `rate_limit_per_minute=1` — messages dispatch as fast as the global
budget allows. Severity **medium** (two knobs that do nothing, documented
as columns). Recommended separate fix: either wire the per-task value into
`_throttle`/`targetRateWait`, or remove the columns.

**DLV-P1 — auto-delete is in-memory on the Python path and durable on the
Go path, and both run.** The Python `_schedule_auto_delete` sleeps in a
task (`durable_queue.py:1826-1829`) — a Core restart loses the deletion
(the scheduler package's own docstring calls this out,
`scheduler/store.go:3-10`). The Go path persists the action
(`delivery.go:1066-1110`). Whichever process owns delivery schedules its
own cleanup; a deployment that runs both (Python worker handing to Go
worker during a migration window) could schedule **two** deletions of the
same message — deletion is idempotent on Telegram's side, so the visible
effect is only a duplicate RPC. Severity **low**. Fixture:
`auto_delete_seconds=60` + job processed by each engine once → two
schedules. Recommended separate fix: one owner flag on the job
(`delivery_stage` or a `scheduled_actions` dedup key).

**DLV-P2 — the Go filter re-evaluation uses the **transformed** payload
text, which the LME/Cleaner/P0 produced; the Python FILTER stage used the
**source** text.** `delivery.go:212` evaluates `j.Payload.Text()`. For a
task whose attached filter has `whitelist_keywords=["buy"]` and whose LME
stripped the only URL, the source matched and the payload still contains
"buy", so both agree. Divergence case: a filter that keys on the *removed*
URL (`blacklist_keywords=["evil.example"]`) — P0 evaluated the source
text at `pipeline.py:918` (pre-transform, per coordinator §0) and *dropped*
it, so no job exists; a filter keyed on text that P0 **added** (header/
footer/watermark) is evaluated by Go but never was by Python. Severity
**low** — the only reachable case is a filter matching the task's own
header, which the coordinator's enqueue-stage semantics also re-decide.
Recorded so a fixture exists: task with attached filter
`blacklist_keywords=["🚀 ATF"]` (the watermark) → Python allow, Go drop at
claim. Recommended separate fix: decide once (either evaluate only the
source text, or document the payload as authoritative) and pin it in a
test.

**DLV-P3 — `is_edit_within_window` is P0-only; the Go preflight has no
edit-time gate.** Jobs are only enqueued for edits that passed the window
(`pipeline.py:824-833`), so a job in the queue is already window-clean.
Not a defect — recorded so the invariant is written down: an edit job
enqueued *before* an `edit_time_limit_seconds` change is delivered even if
the new window would exclude it. Severity **informational**.

**DLV-P4 — `DeliveryStateMachine.resume_after_crash` has no production
caller I could verify.** Searched `resume_after_crash`, `can_transition`
across `core/`; the durable queue and dispatcher drive statuses directly.
The state machine exists as documentation of the legal transitions. Status
`UNREFERENCED_HELPER`. NOT_VERIFIED for the claim "no caller" in contexts
I did not grep (e.g. `atf/`, scripts) — `grep -rn resume_after_crash`
returned only the definition and tests.

### DLV fixtures proposed

* `dlv-task-rate-limit-ignored` — `rate_limit_per_minute=1`, burst 2
  messages 100 ms apart → both dispatch immediately (pins DLV-U1).
* `dlv-auto-delete-double-schedule` — one job, both engines enabled → two
  scheduled actions (pins DLV-P1).
* `dlv-go-filter-sees-header` — attached blacklist on the header text →
  Go drops, Python allowed (pins DLV-P2).
* `lnk-applyto.payload/entities` — the LME fixture from LNK-P1, asserting
  the entity list is clamped and `link_policy=REMOVE_ALL_URLS` leaves the
  inline keyboard untouched (pins LNK-U1).

---

## 6. Control surfaces

Every surface below is mapped against the features D owns. "Bypass" means
the surface writes a store that the engine reads **without** going through
the shared validating service.

### 6.1 Telegram bot (`core/infrastructure/telegram/pro_bot_ui.py`, 13199 lines)

Writes through the shared `ForwardRule` entity and its properties, so
every setting lands in the same stores the engine reads. Outline obtained
by grep of `class`/`def` (never read whole, per the brief):

* Add-on catalogue and toggles: `_ADDON_ROWS` (`pro_bot_ui.py:3253-3271`,
  18 rows incl. `webhook`, `auto_del`, `kw_confirm`, `kw_reply`, `crypto`,
  `affiliate`, `link_buttons`, `watermark`, `ocr_ai`, `ai_rewrite`),
  renderer `_render_all_addons` (`3281-3301`), toggle handler regex
  `radon|radon_tg` (`7372`), writer `rule.paid_addons = …`
  (`7393/7957/8757/8800/10759`).
* Watermark menu `_render_watermark_menu` (`4015-4020`), set prompt
  callback `_cb_rule_watermark_set_prompt` (`8383`), writer
  `rule.watermark = text.strip()` (`10867`), delete `rule.watermark = ""`
  (`8409`).
* Link buttons count + info (`8501-8509`); affiliate toggle
  `_cb_rule_toggle_affiliate` (`8485-8495`, writer
  `rule.affiliate_kit["enabled"]`).
* Transport: token picker (`2293-2362`, writer `rule.bot_token_id` +
  `rule.delivery_transport = "BOT"` at `2360-2362`), route clearing
  (`7477-7491`), validation `_active_bot_token` (`7482`).
* Remove-links toggle (`6658`, `7017-7022`).
* AI: OCR/smart rewrite/news-digest toggles (`7846/7861/7876`), translate
  language (`8439`), creativity cycle (`8521-8525`), AI menu enable writes
  `ai_config_id`, `smart_ai_rewrite` **and** `paid_addons["ai_rewrite"]`
  together (`8752-8760`) — the only surface that keeps the additive gate
  consistent with `entities.py:1679-1699`.

**Bot gap list (features the bot cannot edit):** `link_policy`,
`domain_allowlist`, `domain_blocklist`, `link_rewrite_map`, `auto_post_scheduler`
(beyond the on/off), `caption_max_length`, `split_long_caption`,
`allowed_media_types`, `ai_timeout_seconds`, `ai_fallback_policy`,
`ai_secondary_config_id`, `prompt_id`. These are only reachable via the
REST/gRPC/CLI surfaces or backup.

`replace_bot_ui.py` (1157 lines, `ReplaceBotMixin` mixed in at
`pro_bot_ui.py:426`) owns the **replace-rule** CRUD UI — a separate
`ReplaceRule` aggregate surfaced through `ReplaceRuleControlService`
(`proto/autoforward.proto` `ReplaceRule*` messages). It edits the shared
replace store (`core/domain/text_replace.py` via `apply_replacements`),
so it does **not** bypass the engine's replace authority (Agent B owns
`RPL-*`).

### 6.2 Web dashboard (REST → Go API → gRPC)

Routes (`api/main.go:112-281`): `GET|PUT|PATCH|POST /api/rules…`,
`GET|PUT|PATCH /api/rules/{id}/settings`, `GET /api/rules/settings/schema`,
`POST /api/simulate`, `GET|POST /api/ai-configs…`, `POST /ai-configs/check-key`,
`GET|POST /api/bot-tokens…`, `GET|PUT /bot-tokens/{id}/defaults`,
`POST /api/backup/export|import`, `POST /filters/test`, `GET|POST
/api/prompts…`, `POST /prompts/test`.

Write paths, and which store they hit:

* **`PUT/PATCH /api/rules/{id}` → `UpdateRule`** (`handlers.go:1831-1850`):
  proto fields (incl. `link_policy`, `domain_allowlist`, `domain_blocklist`,
  `remove_links`, `caption_max_length`, `ai_*`) +
  `enrichCustomMetadata` (`handlers.go:436-510`), which copies ~60 flat
  body keys into `custom_metadata_json` (the `parityKeys` list at
  `handlers.go:461-482`) and then `normalizeAdvancedMetadata`
  (`handlers.go:534-640`: typed coercion, `maxWatermarkLen = 200`,
  `maxKeywordReplyLen = 4096`, `maxDelaySeconds = 3600`,
  `maxForwardEveryN = 1000`, `maxMetadataBytes = 64 KiB`).
  **Bypass:** this path does **not** validate against `SETTING_SPECS`
  (`rule_settings.py:96`); unknown keys survive (`handlers.go:535` "never
  deletes unknown keys"), and values the registry would reject (e.g.
  `auto_delete_seconds` > 604800) land as-is. The settings endpoint exists
  precisely to avoid this (`proto/autoforward.proto:213-224`: "Read-only
  here — write through UpdateRuleSettings so values are validated against
  the settings schema").
* **`PUT/PATCH /api/rules/{id}/settings` → `UpdateRuleSettings`**
  (`handlers.go:1539-1575` → gRPC `servicers.py:1297-1424`): the only
  validated batch write. Unknown/invalid values are rejected before
  mutation (`rule_settings.apply_settings` `rule_settings.py:508-595`,
  validate-then-mutate at 540). Route pair enforced (`rule_settings.py:552-569`,
  `servicers.py:1364-1385`): `delivery_transport=BOT` without a token is
  `invalid_state`; clearing `bot_token_id` while on BOT is refused;
  `bot_token_id` ownership checked against the token repository
  (`servicers.py:1319-1332`). `changed`/`reset` semantics implement
  "form posted every field ≠ choice" (`servicers.py:1336-1350`,
  `rule_settings.py:570-592`, `PIN_INDEX`).
* **`GET /api/rules/{id}`** (`handlers.go:380-424`): the join of every
  proto field, `settings_json` (the full snapshot,
  `ForwardRule.settings_json` field 74) and every metadata key
  (`handlers.go:417-422`) — a client can render the whole task from one
  response.

**Web feature gaps:** `link_policy` surfaces only as a read-only column in
the rules table (`web/frontend/src/panels/RulesPanel.tsx:239`) and as a
count of domain-list entries (`TaskOverviewModal.tsx:1130`); no editor.
`link_rewrite_map`, `domain_allowlist`/`blocklist` editors, `auto_delete_seconds`,
`edit_time_limit_seconds` — all reachable only through the generic settings
sheet (generated from the schema, `web/frontend/src/settingLabels.generated.ts:32`
has `auto_delete_seconds`), **not** through `TaskOverviewModal`.
`header/footer` are edited as `custom_header`/`custom_footer`
(`TaskOverviewModal.tsx:1247-1252`) — **not** as `header_template`, see
§6.5.

### 6.3 Mobile (Flutter, `mobile/lib`)

Two distinct writers:

* **`rule_editor_screen.dart`** — the legacy full editor, POST/PUT the
  flat `toCreateJson()` (`mobile/lib/core/models.dart:600-655`). Notably
  sends `'header_template'`/`'footer_template'` (`models.dart:636`)
  — **a key no backend reads** (grep `header_template` outside the app
  returns one website doc, `website/src/data/api-surface.ts:344`). The
  engine reads `custom_header`/`custom_footer`/`metadata["header"]`, so a
  header typed in the Flutter editor is **silently ignored**. Paid add-ons
  toggled as the 10 catalogue keys (`rule_editor_screen.dart:1656-1675`)
  incl. `auto_del` (ADV-P3). `watermark` is sent flat
  (`models.dart:617`) which the API parity list copies into metadata
  (`handlers.go:474`). **Bypass:** the flat PUT path, same as the web.
* **`rule_settings_sheet.dart`** (1434 lines) — the schema-driven sheet:
  loads `ruleSettingsSchema()` + `ruleSettings(id)`, seeds from the
  **effective** (bot-defaults-applied) baseline
  (`rule_settings_sheet.dart:96-140`), diffs locally (`_changed`,
  `156-165`) and sends only real changes through `updateRuleSettings`
  (`386-424`) with client-side min/max validation from the registry
  (`173-210`). The route pair is enforced in the sheet
  (`793`, `931-932`, `981-982`). **This is the only mobile surface that
  can write `link_policy`-adjacent settings** — but since
  `link_policy` is not in `SETTING_SPECS` (LNK-U2), it is still not
  writable here.

**Mobile feature gaps:** no `link_policy`/`domain_*`/`link_rewrite_map`,
no `auto_delete_seconds` field (only the `auto_del` switch), no
`forward_delay_seconds` numeric (only `delaySeconds` flat), no
`ai_timeout_seconds`/`ai_fallback_policy`/`prompt_id`, no
`delivery_transport`/`bot_token_id` in the editor (the token picker lives
in the settings sheet, `rule_settings_sheet.dart:893-932`).

### 6.4 gRPC (`core/infrastructure/grpc_server/servicers.py`, 2950 lines)

Servicers (outline from grep): `SessionControlServicer` (661), `ForwardRuleControlServicer`
(919), `BotTokenControlServicer` (1591), `DeliveryControlServicer` (1800),
`FilterControlServicer` (2187), `AIControlServicer` (2349),
`SystemStatusControlServicer` (2627), `BackupControlServicer` (2814),
`AuditControlServicer` (2889).

* **`UpdateRule`** (`servicers.py:1104-1268`) — mask-based partial update
  (`_apply_masked_update` `servicers.py:293-333`) plus the present-only
  `_V17_*` chain (`servicers.py:200-242` for create; the update side
  mirrors at `1200-1260`). `link_policy`/`domain_allowlist`/
  `domain_blocklist` are wired (`servicers.py:64/72/272-274`).
  `link_rewrite_map` is **not** in the v17 mapping — it is carried only by
  `custom_metadata_json`/`metadata` (and backup).
* **`UpdateRuleSettings`** (`servicers.py:1297-1424`) — the validated
  batch write, identical semantics to the REST wrapper.
* **`TestRule`** (`servicers.py:1447-1548`) — dry run: SmartRouting +
  content policy + all-targets loop check, rendered text from
  `TransformationService.transform_text` (the **legacy** pipeline) —
  parity caveats in HDR-P1/AI-P3.
* **`BackupExport` / `BackupImport`** (`servicers.py:2824-2893`) —
  passphrase-encrypted bundle (`encrypt_bundle`,
  `backup_service.py:713-733`, PBKDF2-HMAC-SHA256 600k iterations
  `backup_service.py:709-711`, `enc:v2:` prefix, v1 refused).

### 6.5 CLI (`atfcli/`)

`atfcli/commands/rules.py`: `list`, `show`, `history`, `create`, `update`,
`delete`, `pause`/`resume`/`stop`/`restart`, `duplicate`, `path`,
`simulate`, `settings schema|get|set|reset`, `sources list|add|remove`.

* **`settings set`** (`rules.py:279-292`) → use case
  `tasks.set_settings` (`atfcli/application/use_cases/tasks.py:239-256`) →
  HTTP gateway `update_settings`
  (`atfcli/infrastructure/http/gateways.py:101-114`) → `PATCH
  /api/rules/{id}/settings`. This is the **validated** path: unknown keys
  come back with the server's `error_code`, and the CLI repeats it
  (`tasks.py:249-254`).
* **`create`/`update`** (`rules.py:105-146`) send
  `sh.settings_from_args(args)` — the `--set key=value` pairs — through
  the same settings verbs? No: they go through `ctx.uc.tasks.create/…`
  (the flat /rules POST/PUT), so a CLI `--set auto_delete_seconds=…`
  bypasses `SETTING_SPECS` validation the same way the dashboard's flat
  PUT does. `settings_from_args` itself accepts list/dict/bool JSON
  (`atfcli/commands/_shared.py:173-187`, with the documented fix that a
  raw list used to be silently dropped).
* **`simulate`** (`rules.py:188-213`) → `POST /api/simulate` → the
  gRPC `TestRule` surface → **legacy** transform output
  (`SimulationResult.transformed_text`,
  `atfcli/domain/models.py:208-232`).
* **backup/restore** (`atfcli/commands/data.py:49-112`):
  `_backup_export` → `POST /api/backup/export`, `_backup_import` →
  `POST /api/backup/import`, `_backup_inspect` local bundle read;
  passphrase from env (`data.py:113-117`). Same gRPC servicers as the web.
* **dead-letter** (`data.py:145-203`): list/retry/retry-all/purge against
  `DeliveryControlService`.

### 6.6 Backup/restore (`core/application/backup_service.py`) + parity table

`create_snapshot` (`backup_service.py:89`) serializes sessions, rules
(`_rule_dict` `167-238`), filters, AI configs, credentials, prompts;
`restore_snapshot` (`305-…`) is idempotent (skip-by-id unless
`overwrite_existing`, `322-328`), re-stamps owners, and restores the
session string **plaintext** (the repo encrypts on write, `401-407`).

`_rule_dict` / `_rule_from_dict` parity for D-owned settings
(`backup_service.py:215-222` export, `613-619` import):

| Setting | Export key | Import key | Round-trips |
|---|---|---|---|
| `link_policy` | ✓ `215` | ✓ `615` | yes |
| `domain_allowlist` | ✓ `216` | ✓ `616` | yes |
| `domain_blocklist` | ✓ `217` | ✓ `617` | yes |
| `link_rewrite_map` | ✓ `218` | ✓ `618` | yes |
| `remove_links` | ✓ `224` | ✓ `610` | yes |
| `split_long_caption` | ✓ `221` | ✓ `620` | yes |
| `caption_max_length` | ✓ `207` | ✓ `607` | yes (unused by any engine) |
| `rate_limit_per_minute`/`burst` | ✓ `198-199` | ✓ `598-599` | yes (unused, DLV-U1) |
| `watermark` / `link_buttons` / `affiliate_kit` / `paid_addons` | via `metadata` `237` | via `metadata` `636` | yes |
| `delivery_transport` / `bot_token_id` | via `metadata` | via `metadata` | yes |
| `auto_delete_seconds` / `edit_time_limit_seconds` / `forward_delay` | via `metadata` | via `metadata` | yes (clamped on read by the entity) |
| `link_replacement`, `header`/`footer` | via `metadata` | via `metadata` | yes |

**Backup-P1 (medium):** `_rule_from_dict` builds `ForwardRule` with
`metadata=raw.get("metadata")` (`backup_service.py:636`) — no validation
against `SETTING_SPECS` and no `PIN_INDEX` reconciliation. A hand-edited
or foreign bundle can set any metadata key (the gRPC layer's only guard
is the passphrase). Combined with REST's `enrichCustomMetadata`
(`handlers.go:436`), there are three unvalidated writers into the same
metadata blob vs. one validated one. Recommended separate fix: run
`normalizeAdvancedMetadata`-equivalent validation inside
`restore_snapshot` (server-side, Python) so the import boundary is as
strict as the settings boundary.

### 6.7 Surface-vs-feature matrix (which surface can read/write what)

`R` = read, `W` = write through the validated settings service, `w` =
write through an unvalidated path (flat `UpdateRule`/metadata/backup),
`—` = not surfaced.

| Feature | Bot | Web | Mobile | gRPC | CLI | Backup |
|---|---|---|---|---|---|---|
| watermark / link_buttons / affiliate_kit | R/W | R/w (modal) + W (sheet) | R/w (editor) + W (sheet) | R/w | W (settings set) | R/w |
| paid_addons (incl. `auto_del` spelling bug) | R/W | R/W | R/W | R/w | W | R/w |
| link_policy / domain_* / link_rewrite_map | — | R only | — | R/W (not rewrite map) | — | R/w |
| remove_links / link_replacement | R/W | R/w | R/w | R/W | W | R/w |
| header / footer (placeholders) | R/W (legacy fmt) | R/w (`custom_header`) | R/w (**`header_template` — broken**) | R/W | W | R/w |
| AI: config/secondary/prompt/timeout/fallback | R/W (menu) | R/W (settings) | R (settings sheet) | R/W | W | R/w |
| `translate_lang`, `ai_creativity`, `ai_max_tokens`, `ocr_image_ai`, `news_digest`, `crypto_mode` | R/W | R/w + W | R/w | R/w | W | R/w |
| delivery_transport / bot_token_id | R/W | R/W (validated pair) | R/W (sheet) / w (editor) | R/W (pair rule enforced) | W | R/w |
| auto_delete_seconds / edit_time/window / forward_delay | R/W | W (sheet) / w (modal) | — / w / w | R/w | W | R/w |
| rate_limit_per_minute / burst | R (display) | R (display) | — | R/W | — | R/w |
| caption_max_length / split_long_caption | — | — | — | R/W | — | R/w |

**Surfaces that write a setting no engine reads** (the brief asks
explicitly): `rate_limit_per_minute`, `rate_limit_burst` (DLV-U1 — every
surface), `translate_lang` (AI-P1), `ai_creativity`, `ai_max_tokens`
(AI-P4), `ocr_image_ai`, `news_digest`, `crypto_mode` (§7), `webhook`
(§7), `caption_max_length` (§2.3), and **`header_template`/`footer_template`
from Flutter** (§6.3 — a write that lands nowhere). Also `paid_addons["watermark"]`
(ADV-P2) and `paid_addons["auto_del"]` (ADV-P3) — writes that land but do
not reach the engine's key.

---

## 7. Stored-but-unused catalogue (Agent D scope)

All persisted, all surfaced, none read by a production engine path:

| Setting | Store | Writers | Engine readers | Evidence of absence |
|---|---|---|---|---|
| `ocr_image_ai` | `metadata` (`entities.py:1659`) | bot `7846`, mobile, web, API parity `handlers.go:474` | none | `grep ocr_image_ai core/` → constants, settings, UI, backup — no pipeline hit |
| `news_digest` | `metadata` (`entities.py:1701`) | bot `7869`, mobile, web | none | same |
| `crypto_mode` | `metadata` (`entities.py:1431`) | bot `7801`, web, mobile | none — `CRYPTO_ADDON_KEY` is defined but never read (`services.py:1429` vs the four read calls) | same |
| `webhook` | `paid_addons["webhook"]` | bot `_ADDON_ROWS` `3259`, web `addons.ts` (via locked list), mobile | none — no webhook dispatch anywhere in `core/` (grep outside i18n returns only the bot's UI rows) | `pro_bot_ui.py:2058/2066/2095/2108/3259` |
| `translate_lang` | `metadata` (`entities.py:1582`) | bot `8439`, settings registry `180` | none (AI-P1) | `pipeline.py:1672` hard-codes `"auto"` |
| `ai_creativity` / `ai_max_tokens` | `metadata` (`1629`, `1644`) | bot `3170`, mobile `618`, settings `191-204` | none — providers use `config.temperature` (`providers.py:161`) | AI-P4 |
| `caption_max_length` | column `251` | gRPC round-trip, backup | none | §2.3 |
| `rate_limit_per_minute` / `rate_limit_burst` | columns `241-242` | gRPC, backup, display everywhere | none | DLV-U1 |
| `process_raw_text` / `button_to_text` | `metadata` (`1561`, `1571`) | bot, web, mobile, payload `durable_queue.py:371-372` | payload **carried** into the job; no reader in either dispatcher found | NOT_VERIFIED (see Unknowns) |

---

## 8. Unknowns (could not be verified, and why)

1. **`TelegramMessageCapabilityMatrix`** (`services.py:1188-1250`) — I
   did not finish grepping every consumer of the capability dict; it may
   drive a UI hint. Status `NOT_VERIFIED` between "documentation table"
   and `UNREFERENCED_HELPER`.
2. **`process_raw_text` / `button_to_text` consumers inside the job.**
   Both are written into the payload (`durable_queue.py:371-372`); I did
   not find the branch in `_dispatch_to_telegram` that consumes them
   (searched 1957-2270 for `process_raw_text`/`button_to_text` → no hit).
   Either dead payload keys or consumed via a helper I did not trace.
   `NOT_VERIFIED`.
3. **`DeliveryStateMachine` callers** — `grep resume_after_crash`/
   `can_transition` returned no production caller; the possibility of a
   caller in `atf/`, `scripts/` or the replay path I did not exhaustively
   read. `NOT_VERIFIED`.
4. **The Go `transform.Engine` is constructed but never called**
   (`delivery.go:120`, `transforms` field unused elsewhere). I verified by
   grep that no call site exists in `worker/`; the possibility of a
   conditional build tag or an in-progress branch makes this
   `NOT_VERIFIED` rather than certain dead code — but on the audited
   baseline it is unreachable.
5. **`session-side` (MTProto) retry policy** — `retry_policy` /
   `max_retries` / `retry_backoff_base` are read by the Go failure path
   (which uses its own `backoffFor`, `delivery.go:805-815`) — whether the
   Python `_dispatch_to_telegram` honours `max_retries` I did not fully
   trace (the Python retry logic lives in `_dispatch_to_telegram`'s
   failure tail, which I only sampled). `NOT_VERIFIED`.
6. **`since_ts` / `skip_history` interactions with delivery** are Agent
   C's (`EVT-*`); I deliberately did not document them.
7. **`docs/feature_catalog.json`** was not opened (hard rule).

---

## 9. Fixtures proposed (coordinator builds the JSON)

| id | one-line description |
|---|---|
| `adv-watermark-no-addon-gate` | `paid_addons["watermark"]=false` + `watermark_text` → watermark still appended (ADV-P2) |
| `adv-auto-delete-addon-key-mismatch` | `paid_addons={"auto_del": true}` + TTL → TTL resolves to 0 (ADV-P3) |
| `adv-auto-delete-bound-by-surface` | 90 000 000 s TTL via REST vs settings API vs engine clamp (ADV-P1) |
| `adv-delay-bound-by-surface` | 7200 s delay via REST metadata vs settings API (ADV-P4) |
| `hdr-placeholder-durable-vs-preview` | header `{date}` → literal in P0, rendered in simulate (HDR-P1) |
| `lnk-inline-keyboard-not-filtered` | REMOVE_ALL_URLS with a URL button → text stripped, button kept (LNK-U1) |
| `lnk-remove-links-preserves-mention` | `remove_links=true` + `@seller` → LME keeps the mention **and** ContentPolicy keeps it (LNK-P3, pins the current `remove_urls` choice) |
| `lnk-rewrite-map-subdomain` | `link_rewrite_map={"a.com":"b.com"}` on `sub.a.com` → rewritten (LNK-01/05 positive) |
| `lnk-unknown-policy-fails-closed` | `link_policy="STRIP_ALL"` → PRESERVE_ALL, one warning (LNK-01) |
| `ai-translate-lang-ignored` | `translate_lang="fa"` → request still `"auto"` (AI-P1) |
| `ai-fallback-retry-equals-drop` | RETRY policy + failure → dropped, no retry (AI-P2) |
| `dlv-task-rate-limit-ignored` | `rate_limit_per_minute=1` → no throttle (DLV-U1) |
| `dlv-auto-delete-double-schedule` | both engines → two scheduled deletions (DLV-P1) |
| `dlv-go-filter-sees-transformed-text` | attached blacklist matching the header → Go drops (DLV-P2) |
| `mobile-header-template-write-lost` | Flutter `header_template` → engine sees empty `custom_header` (§6.3) |
| `backup-metadata-unvalidated` | bundle metadata with `auto_delete_seconds=10**9` → imported as-is (Backup-P1) |

---

## 10. Severity roll-up

| Severity | Count | IDs |
|---|---|---|
| high | 1 | ADV-P3 (auto-delete add-on key mismatch — feature silently off) |
| medium | 8 | ADV-P1, ADV-P2, ADV-P4, HDR-P1, LNK-U1, LNK-U2, AI-P1, AI-P2, DLV-U1, Backup-P1 (10 entries; two are consolidation overlaps) |
| low | 4 | LNK-P2, AI-P4, DLV-P1, DLV-P3 |
| informational | 5 | LNK-P1 (documented design), LNK-P3 (verified non-divergence, pinned), AI-P3, DLV-P3, DLV-P4 |
| unreferenced | 4 | LNK-U1, AI store §7 table, `DeliveryStateMachine`, Go `transform.Engine` |

Every recommended fix above is a **separate PR**; none requires touching
another agent's feature IDs.
