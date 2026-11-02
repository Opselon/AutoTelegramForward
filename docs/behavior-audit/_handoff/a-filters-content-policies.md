# Agent A — Filters & Content Policies (FLT / POL / MED / RT / SET)

Stage 4.0 discovery-only audit. Baseline master `35ee530cc80cd17d1ac7d702c4e5f5ea8024889a`,
branch `audit/complete-business-logic-inventory`. Every claim below carries `file:line`
evidence measured against that SHA. Nothing outside this file was modified; no production
code was written; no commits were made.

Engine boundaries use the coordinator's four-engine vocabulary verbatim
(`COORDINATOR_VOCABULARY.md:14-36`): this file owns **Engine 1** (`FilterEngine.evaluate`,
attached `FilterRule`), **Engine 2** (`ContentPolicy.evaluate` / `.transform()`), the shared
media gate (`media_filter.py`) and rule matching (`SmartRoutingEngine`). The Cleaner
(Engine 3) and `LinkManagementEngine` (Engine 4) are B/D territory and appear here only as
the boundary a filter hands off to.

---

## 1. SETTING_SPECS inventory (SET-*)

Registry opens at `core/domain/rule_settings.py:96`; 76 entries confirmed by AST parse of
the module (`AnnAssign` `SETTING_SPECS`, 76 key/value pairs). Schema version 1
(`rule_settings.py:33`). All reads/writes go through the entity property, never by poking
`metadata` directly (`rule_settings.py:9-11`).

**Deliberate exclusions** documented in the module docstring
(`rule_settings.py:13-16`): first-class `ForwardRule` fields — `forward_mode`,
`content_mode`, `delay_seconds`, `sync_edits`, `prompt_id` — are deliberately NOT listed
because "a second write path could disagree with the first". The branding pair
`use_intermediate` / `intermediate_channel_id` is excluded for the same reason
(`rule_settings.py:284-287`).

**Group counts** (from AST): cleanup 13, content 12, sending 11, media 10, ai 7,
automation 7, branding 7, audience 4, keywords 3, route 2. Note `_GROUP_ORDER`
(`rule_settings.py:311`) also lists `general`, which has **zero** members — no setting
uses the default group.

### 1a. Full key table

Each row: key · kind · default · group · inheritable · storage · entity property · runtime
reader · phase · Go parity · gap. "Column" = first-class `ForwardRule` dataclass field;
"meta" = `metadata[...]` property. Entity line numbers are the `def` in
`core/domain/entities.py` (`@property` decorators sit on the preceding line).

#### Group: cleanup (13) — CLN/RPL/D-owned engines, SET documents the registry semantics

| # | key | kind | default | inh | storage | entity property | runtime reader | phase | Go | gap |
|---|---|---|---|---|---|---|---|---|---|---|
| SET-001 | `header` `RS:98` | str | `""` | yes | meta (`custom_header` mirror) | `entities.py:589` (falls back to `metadata["header"]`) | `pipeline.py:1547-1552` header/footer step; `FilterEngine.transform_text` `services.py:302-312` | TRANSFORM | PARTIAL (`transform.go:144` `FormatText` is the placeholder renderer only, no payload interpolation) | Go `FormatText` does not know `ORIGIN_*` placeholders (`services.py:358-379` defines 16) |
| SET-002 | `footer` `RS:100` | str | `""` | yes | meta | `entities.py:619` | `pipeline.py:1547-1552` | TRANSFORM | PARTIAL | same as header |
| SET-003 | `replacements` `RS:102` | dict | `{}` | yes | meta | `entities.py:576` | `text_replace.apply_replacements` via `pipeline.py:1543` (B/RPL ownership) | TRANSFORM | ABSENT | Go worker never re-applies (baked into payload, per `transform.go:4-11`) — by design |
| SET-004 | `link_replacement` `RS:110` | str | `""` | yes | meta | `entities.py:1859` | `pipeline.py:1456` `FilterEngine.replace_links` (B-owned) | TRANSFORM | ABSENT (by design) | — |
| SET-005..011 | `clean_usernames` `111` / `clean_hashtags` `112` / `clean_phones` `113` / `clean_buttons` `114` / `clean_empty_lines` `115` / `clean_custom_words` `116` / `remove_emojis` `119` | bool/list | `False`/`[]` | yes | meta | `entities.py:690/700/710/720/730/740/765` | CleanerRule via `build_cleaner` (`entities.py:808`) at `pipeline.py:1408`; `remove_emojis` also read directly at `pipeline.py:1461` | TRANSFORM | ABSENT (by design) | B/D scope; documented here only as registry row |
| SET-012 | `cleaner_enabled` `RS:120` | bool | `False` | yes | meta | `entities.py:936` (also synthesizes True when any legacy cleaner flag is set, `entities.py:940-943`) | `build_cleaner` gate `entities.py:808` | TRANSFORM | ABSENT by design | master switch semantics |
| SET-013 | `cleaner_config` `RS:122` | dict | `{}` | yes | meta | `entities.py:1005` | `build_cleaner` `entities.py:808` | TRANSFORM | ABSENT by design | — |

#### Group: media (10) — MED-*, boundary rows handed to CLN/LNK

| # | key | kind | default | inh | storage | entity property | runtime reader | phase | Go | gap |
|---|---|---|---|---|---|---|---|---|---|---|
| SET-014 | `block_text` `RS:126` | bool | `False` | yes | meta | `entities.py:670` | `media_filter.media_block_reason` `media_filter.py:77` ← `pipeline.py:956` | FILTER | ABSENT | Go worker does not re-check task media flags at delivery; flags ran on ingest |
| SET-015 | `block_voice` `RS:127` | bool | `False` | yes | meta | `entities.py:630` | same as SET-014 | FILTER | ABSENT | covers `("voice","audio")` `media_filter.py:57` |
| SET-016 | `block_audio` `RS:128` | bool | `False` | yes | meta | `entities.py:660` | same | FILTER | ABSENT | covers `("audio","voice")` `media_filter.py:58` |
| SET-017 | `block_video` `RS:129` | bool | `False` | yes | meta | `entities.py:650` | same | FILTER | ABSENT | covers `("video","video_note")` `media_filter.py:59` |
| SET-018 | `block_stickers` `RS:130` | bool | `False` | yes | meta | `entities.py:640` | same | FILTER | ABSENT | `("sticker",)` `media_filter.py:60` |
| SET-019 | `block_links` `RS:131` | bool | `False` | yes | meta | `entities.py:680` | `media_filter.link_block_reason` `media_filter.py:94` ← `pipeline.py:956` (task-level); folded into ContentPolicy at `entities.py:1364`; and FilterRule gate `services.py:79` (attached filter, different store) | FILTER | PARTIAL | see POL/FLT gap: three `block_links` stores, `link_block_reason` uses `has_link` (broader) while FilterEngine gate uses `URL_REGEX`+substring fallbacks — see P2 |
| SET-020 | `allowed_media_types` `RS:137` | list/str | `[]` | yes | **COLUMN** `entities.py:407` | (field, not property) | `media_filter.media_block_reason` `media_filter.py:70-87`; FilterRule `services.py:73` reads the attached FilterRule's own copy | FILTER | ABSENT | comment `rule_settings.py:133-136` explains why it is a column (backups/REST already carry it) |
| SET-021 | `album_mode` `RS:139` | str | `"album"` | yes | meta | `entities.py:818` | album aggregation (C-owned) | ENQUEUE | PARTIAL (`rule.go:78` carries `AlbumMode`) | — |
| SET-022 | `watermark` `RS:141` | str | `""` | yes | meta | `entities.py:1592` | AdvancedSettingsEngine (D-owned) | TRANSFORM | ABSENT | — |
| SET-023 | `watermark_text` `RS:142` | str | `""` | yes | meta | `entities.py:1831` | AdvancedSettingsEngine | TRANSFORM | ABSENT | — |

#### Group: sending (11) — FEN/EVT/D-owned

| # | key | kind | default | inh | storage | entity property | runtime reader | phase | Go | gap |
|---|---|---|---|---|---|---|---|---|---|---|
| SET-024 | `duplicate_filters` `RS:145` | bool | `False` | yes | meta | `entities.py:922` | `pipeline.py:860+` content-hash claim (DUP-*, C-owned) | DEDUPLICATE | ABSENT | — |
| SET-025 | `max_time_edit` `RS:147` | int | 86400 | yes | meta | `entities.py:753` | edit sync window (C-owned) | DEDUPLICATE | ABSENT | bounds 0..2592000 `RS:148` |
| SET-026 | `edit_time_limit_seconds` `RS:150` | int | 0 | yes | meta | `entities.py:1768` | `AdvancedSettingsEngine.is_edit_within_window` `services.py:1540-1556` | DEDUPLICATE `pipeline.py:820` | ABSENT | 0 = "no window, all edits sync" `services.py:1546-1548` |
| SET-027 | `forward_delay_seconds` `RS:152` | float | 0.0 | yes | meta→COLUMN | `entities.py:475-477` promotes into `delay_seconds` field in `__post_init__` | `AdvancedSettingsEngine.get_dispatch_delay` `services.py:1558+` | ENQUEUE | PARTIAL (`rule.go` has no delay; Go defers via store) | — |
| SET-028 | `auto_delete_seconds` `RS:154` | int | 0 | yes | meta | `entities.py:1812` | `AdvancedSettingsEngine.get_auto_delete_seconds` `services.py:1528-1538` (addon-gated) | post-DELIVERY | PARTIAL (worker schedules auto-delete, D-owned) | — |
| SET-029 | `forward_every_n` `RS:161` | int | 1 | yes | meta | `entities.py:345` (clamped on read AND write, `entities.py:370-376`) | `pipeline.py:1025+` slot gate | FORWARD_INTERVAL | ABSENT | deliberate: counter is ingest-side; Go sees only jobs that survived it |
| SET-030 | `debug_mode` `RS:169` | bool | `False` | yes | meta | `entities.py:901` | **no engine reader found** — only bot UI (`pro_bot_ui.py:1895,7412`) | (UI only) | ABSENT | STORED_BUT_UNUSED for decision logic; may gate logging elsewhere (not verified pipeline-wide) |
| SET-031 | `sync_replies` `RS:171` | bool | `False` | yes | meta | `entities.py:1421` | C/D-owned delivery path | ENQUEUE | ABSENT | — |
| SET-032 | `process_raw_text` `RS:173` | bool | `False` | yes | meta | `entities.py:1561` | NOT_VERIFIED — no reader in `pipeline.py`/`services.py` found | — | ABSENT | — |
| SET-033 | `feature_group` `RS:175` | str | `"all"` | yes | meta | `entities.py:1725` | only bot UI grouping (`pro_bot_ui.py:1938`); no no-choices constraint (`choices=()` — registry accepts any string) | UI only | ABSENT | registry enforces nothing; a Typo'd group is silently stored |
| SET-034 | `sender_type` `RS:176` | str | `"Personal Telegram Account"` | yes | meta | `entities.py:911` | no engine reader found (UI display only, `pro_bot_ui.py`) | UI only | ABSENT | default is a human sentence, not an enum — `coerce_value` accepts any string |

#### Group: keywords (3) — KW-*, C-owned

| # | key | kind | default | inh | storage | entity property | runtime reader | phase | Go | gap |
|---|---|---|---|---|---|---|---|---|---|---|
| SET-035 | `keyword_confirmation` `RS:179` | bool | `False` | yes | meta | `entities.py:1787` | `AdvancedSettingsEngine.classify_keyword_trigger` `services.py:1503-1526` | ENQUEUE `pipeline.py:1315-1334` | ABSENT | C-owned; documented for boundary |
| SET-036 | `keyword_reply` `RS:180` | str | `""` | yes | meta | `entities.py:1799` | same `services.py:1507` | ENQUEUE | ABSENT | — |
| SET-037 | `translate_lang` `RS:181` | str | `""` | yes | meta | `entities.py:1582` | **no engine reader found** — only bot UI (`pro_bot_ui.py:4045,8439`); translation appears to run on the AI path (NOT_VERIFIED) | (UI/other) | ABSENT | help says "Target language for auto-translation" `RS:182` but no pipeline read verified |

#### Group: audience (4) — FLT/POL boundary, see §4

| # | key | kind | default | inh | storage | entity property | runtime reader | phase | Go | gap |
|---|---|---|---|---|---|---|---|---|---|---|
| SET-038 | `blacklist` `RS:185` | list/str | `[]` | yes | meta, aliases policy blob | `entities.py:1451` (compat view over `blacklist_words`, pops legacy `metadata["blacklist"]` on write `entities.py:1464`) | keyword-reply trigger `services.py:1514` (substring, lowercased — NOT `contains_word`); legacy fold into policy `entities.py:1372-1377` | FILTER (via policy) | ABSENT | a *third* semantic for the same word: keyword reply uses raw `kw in haystack` while the policy uses `contains_word` — see §4 |
| SET-039 | `whitelist` `RS:187` | list/str | `[]` | yes | meta, aliases policy blob | `entities.py:1467` | keyword-reply trigger `services.py:1514`; legacy fold `entities.py:1375-1377` | FILTER (via policy) | ABSENT | same |
| SET-040 | `blacklist_users` `RS:189` | list/str | `[]` | yes | meta | `entities.py:1483` | **no engine reader found anywhere in `core/application` or `core/domain`** (`grep` over pipeline/services/use_cases: zero hits; only bot UI counts `pro_bot_ui.py:3085,8044`) | NONE | ABSENT | STORED_BUT_UNUSED — cannot reject a message. See §4 item 4 |
| SET-041 | `whitelist_users` `RS:192` | list/str | `[]` | yes | meta | `entities.py:1496` | same: no engine reader | NONE | ABSENT | STORED_BUT_UNUSED — see §4 item 4 |

#### Group: ai (7) — AI-*, D-owned

| # | key | kind | default | inh | storage | entity property | runtime reader | phase | Go | gap |
|---|---|---|---|---|---|---|---|---|---|---|
| SET-042 | `ai_creativity` `RS:196` | float | 0.7 | yes | meta | `entities.py:1629` | AI transform (D-owned) `pipeline.py:1524` | TRANSFORM | ABSENT | bounds 0..2 |
| SET-043 | `ai_max_tokens` `RS:199` | int | 600 | yes | meta | `entities.py:1644` | AI transform | TRANSFORM | ABSENT | bounds 1..100000 |
| SET-044 | `ocr_image_ai` `RS:201` | bool | `False` | yes | meta | `entities.py:1659` | addon gate `services.py:1429` (`OCR_ADDON_KEY` `services.py:1433`) | TRANSFORM | ABSENT | — |
| SET-045 | `news_digest` `RS:203` | bool | `False` | yes | meta | `entities.py:1701` | NOT_VERIFIED reader (bot UI only verified) | — | ABSENT | — |
| SET-046 | `smart_ai_rewrite` `RS:204` | bool | `False` | yes | meta | `entities.py:1669` | AI rewrite gate `pipeline.py:1524` | TRANSFORM | ABSENT | — |
| SET-047 | `crypto_mode` `RS:206` | bool | `False` | yes | meta | `entities.py:1431` | addon gate `services.py:1429` (`CRYPTO_ADDON_KEY`) | — | ABSENT | — |
| SET-048 | `paid_addons` `RS:207` | dict | `{}` | yes | meta | `entities.py:1735` | `is_addon_enabled` (all addon gates) | all phases | ABSENT | the addon permission layer — every `*_ADDON_KEY` gate reads this |

#### Group: route (2) — NOT inheritable

| # | key | kind | default | inh | storage | entity property | runtime reader | phase | Go | gap |
|---|---|---|---|---|---|---|---|---|---|---|
| SET-049 | `delivery_transport` `RS:213` | str | `"USER"` | **NO** | meta | `entities.py:851` | transport selection (D-owned) + `apply_settings` pair-validation `rule_settings.py:558-566` | ENQUEUE | COMPLETE (`rule.go:62` `DeliveryTransport`, `delivery.go` uses it) | pair-applied with `bot_token_id` (`rule_settings.py:568-570` ordering) |
| SET-050 | `bot_token_id` `RS:216` | str | `""` | **NO** | meta | `entities.py:829` | transport resolution | ENQUEUE | COMPLETE (`rule.go:68`) | secret never exposed (`RS:218-220`) |

#### Group: automation (7) — NOT inheritable except `button_to_text`

| # | key | kind | default | inh | storage | entity property | runtime reader | phase | Go | gap |
|---|---|---|---|---|---|---|---|---|---|---|
| SET-051 | `auto_post_scheduler` `RS:223` | dict | `{"enabled": False}` | NO | meta | `entities.py:1510` | scheduler (not pipeline path) | — | ABSENT | — |
| SET-052 | `auto_posting` `RS:225` | bool | `False` | NO | meta | `entities.py:1523` | view of `auto_post_scheduler.enabled` | — | ABSENT | — |
| SET-053 | `power_control` `RS:227` | bool | `False` | NO | meta | `entities.py:1548` | view of `power_schedule.enabled` | — | ABSENT | — |
| SET-054 | `power_schedule` `RS:229` | dict | `{"enabled": False}` | NO | meta | `entities.py:1535` | — | — | ABSENT | — |
| SET-055 | `button_to_text` `RS:231` | bool | `False` | yes | meta | `entities.py:1571` | NOT_VERIFIED reader | — | ABSENT | only inheritable automation key |
| SET-056 | `link_buttons` `RS:232` | list/str | `[]` | NO | meta | `entities.py:1602` | AdvancedSettingsEngine `services.py:1469` | TRANSFORM | ABSENT | — |
| SET-057 | `affiliate_kit` `RS:234` | dict | `{}` | NO | meta | `entities.py:1615` | AdvancedSettingsEngine `services.py:1481` | TRANSFORM | ABSENT | — |

#### Group: content (12) — POL-*, this agent's primary

| # | key | kind | default | inh | storage | entity property | runtime reader | phase | Go | gap |
|---|---|---|---|---|---|---|---|---|---|---|
| SET-058 | `content_policy_enabled` `RS:240` | bool | `False` | yes | policy blob | `entities.py:1166` (falls back to `has_rules` when key absent, `entities.py:1171`) | `ContentPolicy.is_active` `content_policy.py:791-797` via `enabled` in blob | FILTER | ABSENT | registry default `False` vs policy default `None` (auto) — see POL-001 |
| SET-059 | `blacklist_off` `RS:248` | bool | `False` | yes | policy blob | `entities.py:1182` | `ContentPolicy.evaluate` `content_policy.py:847` | FILTER | ABSENT | v30 per-list switch |
| SET-060 | `whitelist_off` `RS:251` | bool | `False` | yes | policy blob | `entities.py:1190` | `content_policy.py:852` | FILTER | ABSENT | also parks `regex_require` (`content_policy.py:886`) |
| SET-061 | `blacklist_words` `RS:254` | list/str | `[]` | yes | policy blob (+ flat/legacy merge on read `entities.py:1124-1138`) | `entities.py:1198` | `content_policy.py:847-850` | FILTER | ABSENT | — |
| SET-062 | `whitelist_words` `RS:257` | list/str | `[]` | yes | same | `entities.py:1206` | `content_policy.py:852-868` | FILTER | ABSENT | — |
| SET-063 | `whitelist_mode` `RS:260` | str | `"any"` | yes | policy blob | `entities.py:1230` (validates to any/all/phrase `entities.py:1232`) | `content_policy.py:853-868` | FILTER | ABSENT | choices enforced both in registry and entity |
| SET-064 | `remove_links` `RS:265` | bool | `False` | yes | **COLUMN** `entities.py:402` + folded into policy `entities.py:1366-1367` | (field) | `ContentPolicy.transform` `content_policy.py:914-920` → `FilterEngine.remove_urls`; ALSO the LME gate `pipeline.py:1448` (D-owned) | TRANSFORM | PARTIAL (`transform.go:62` `RemoveLinks` for target override only) | three readers of one flag — see POL-006 |
| SET-065 | `strip_words` `RS:268` | list/str | `[]` | yes | policy blob | `entities.py:1239` | `content_policy.py:922-929` `_strip_word` | TRANSFORM | PARTIAL (`transform.go:68-75` `StripWords`, literal `strings.ReplaceAll`) | Go strip is literal/substring; Python is variant+boundary aware — parity finding P-STRP |
| SET-066 | `regex_block` `RS:271` | list/str | `[]` | yes | policy blob, healed on read `entities.py:1215` | `entities.py:1214` | `content_policy.py:873-884` | FILTER | ABSENT | registry validates each pattern at write (`rule_settings.py:376-383`) |
| SET-067 | `regex_require` `RS:274` | list/str | `[]` | yes | policy blob, healed `entities.py:1223` | `entities.py:1222` | `content_policy.py:886-901` | FILTER | ABSENT | same |
| SET-068 | `source_policies` `RS:277` | dict | `{}` | **NO** | policy blob | `entities.py:1247` | `policy_chain` `entities.py:1397-1401` ← `pipeline.py:974-984` | FILTER | ABSENT | additive-only (comment `entities.py:1095-1097`) |
| SET-069 | `target_policies` `RS:280` | dict | `{}` | **NO** | policy blob | `entities.py:1256` | `policy_chain` `entities.py:1402-1406` ← `pipeline.py:1197-1205` (evaluate) and `pipeline.py:1220-1229` (transform) | ENQUEUE | PARTIAL (`transform.go:42-49` `TargetOverride{RemoveLinks,StripWords}`) | Go override is a **projection**: only RemoveLinks/StripWords survive the Go path; a target policy with blacklist words is NOT re-decided by Go — matches Python intent (only `target:`-prefixed policies re-decided, `pipeline.py:1200`), but Go cannot evaluate word lists even if asked |
| *(shared)* | `block_links` is registered exactly once, at `RS:131`, under the **media** group — the content group carries an explicit note at `RS:263-264` that it is the same entity property and no second registration exists. Documented at SET-019. | | | | | | | | | |

(`block_links` is registered exactly once, at `RS:131`, and the content group carries an
explicit note at `RS:263-264` that it is the same entity property.)

#### Group: branding (7) — BRD-*, C/D-owned; registry rows listed for completeness

| # | key | kind | default | inh | storage | entity property | runtime reader | phase | Go | gap |
|---|---|---|---|---|---|---|---|---|---|---|
| SET-070 | `branding_triggers_enabled` `RS:288` | bool | `False` | yes | branding blob | `entities.py:1296` | `branding.should_hop` via `SmartRoutingEngine.should_brand_hop` `services.py:576-589` | ROUTE | COMPLETE (`branding.go`) | D/C-owned |
| SET-071..076 | `branding_origin_titles` `291` / `branding_origin_ids` `294` / `branding_keywords` `297` / `branding_match_mode` `300` (choices any/all) / `branding_require_forward` `303` / `branding_force_forward` `306` (default **True**) | list/str/bool | `[]`/`[]`/`[]`/`"any"`/`False`/`True` | yes | branding blob | `entities.py:1314/1305/1323/1332/1341/1350` | `branding.should_hop` | ROUTE | COMPLETE | parity fixtures exist at `worker/internal/parity/fixtures` + `branding_parity_test.go` |

**Total: 76 distinct keys** — SET-001 through SET-069 in the groups above, plus the six
branding rows SET-070..SET-075/6 (the `branding_*` group has 7 keys: SET-070 +
SET-071..076 covers `triggers_enabled`, `origin_titles`, `origin_ids`, `keywords`,
`match_mode`, `require_forward`, `force_forward`), with `block_links` shared between the
media and content groups under one registration.

### 1b. Registry semantics worth pinning (SET-*)

- **`coerce_value` fails closed on unknown/invalid** — `unknown_setting` `RS:327`,
  `invalid_type` `RS:341/345/350/355/367/390`, `out_of_range` `RS:399-404`, and
  `invalid_choice` `RS:357-362`. Bool is checked before int because `True` is an `int`
  in Python (`RS:335-336`). Comment: "an API that trusted the wire would be able to
  store values the bot itself would refuse" (`RS:322-324`).
- **Regex patterns validated at write time**: `regex_block`/`regex_require` list items
  are compiled through `compile_user_regex` and rejected with `invalid_choice` when
  unusable (`RS:376-383`) — so the *stored* policy can never carry an un-compilable
  pattern, while the engine still fails closed defensively (`content_policy.py:876-882`).
- **`apply_settings` validates everything before mutating** (`RS:548-549`): a rejected
  request never leaves the rule half-updated.
- **Route pair atomicity** (`RS:553-566`): `delivery_transport=BOT` without
  `bot_token_id` raises `invalid_state`; the token is applied before the transport
  (`RS:568-570`) because "the setter only accepts BOT once a token is present".
- **PIN_INDEX (`_pinned_settings`, `RS:414`)**: only keys a write *moved* are recorded
  (`RS:584-585`), so "a client that posts a whole settings object does not pin the
  settings it merely rendered". `clear_setting` deletes the metadata entry rather than
  writing the default, "otherwise the task would stop tracking the bot" (`RS:470-474`).
- **Inheritable=False means "never inherited from a bot default"** (`RS:59-64`): chat,
  route and schedule settings. `delivery_transport`, `bot_token_id`,
  `auto_post_scheduler`, `auto_posting`, `power_control`, `power_schedule`,
  `link_buttons`, `affiliate_kit`, `source_policies`, `target_policies` — 10 keys.
- **Read surfaces**: gRPC settings JSON `servicers.py:505/1291/1334/1351/1367`,
  `apply_settings` at `servicers.py:1391`; bot `registry_editor.py:42-44`; bot defaults
  `bot_defaults.py:33-34/96-147`; effective-settings merge
  `effective_settings.py:95-105`.

---

## 2. FilterRule evaluation contract (FLT-*)

`FilterEngine.evaluate` — `core/domain/services.py:46-140`. Verified production call path:

```
DurableMessagePipeline.process_message            (pipeline.py:689 routing → 762 handle)
  → _filter_engine.evaluate(payload, filter_rule) (pipeline.py:918)
     ← filter_rule fetched by rule.filter_rule_id   (pipeline.py:889-891)
     ← ownership guard: filter.owner_user_id must be 0 or the task's owner
        (pipeline.py:903-916; a foreign filter is ignored, filter_rule=None)
```
Second production path (Go Worker, delivery time):
```
delivery.Engine.execute → e.filters.Evaluate(text, media, false, f)   (delivery.go:212)
  f from rule.Store.GetByID → rulestore delivery row projection       (rule.go:84-101)
```

### 2a. Exact evaluation order (verified line by line)

| # | check | evidence | drop reason |
|---|---|---|---|
| 0 | `payload.is_service` → drop when no filter attached, or when `filter_rule.drop_service_messages` (default **True**) | `services.py:47-51` | `service_message_dropped` |
| 1 | `filter_rule is None` → ALLOW | `services.py:53-54` | — (reason `no_filter`) |
| 2 | `is_enabled` False → ALLOW (v30: skipped, never evaluated and never deleted) | `services.py:59-60` | — (reason `filter_disabled`) |
| 3 | `blocked_media_types` contains `media` (lowercased) | `services.py:64-67` | `media_blocked:<media>` |
| 3b | `blocked_media_types` contains the literal string `"link"` → link sweep on `effective_text` | `services.py:68-71` | `link_blocked` |
| 4 | `allowed_media_types` non-empty and media not a member (exact or case-insensitive) | `services.py:73-74` | `media_not_allowed:<media>` |
| 5 | `block_links` → URL/substring sweep | `services.py:79-81` | `link_blocked` |
| 6 | `min_message_length` and `len(text) < min` | `services.py:83-84` | `too_short` |
| 7 | `max_message_length` and `len(text) > max` | `services.py:86-87` | `too_long` |
| 8 | blacklist keywords — ANY match (`any(...)`, OR semantics) | `services.py:89-90` | `blacklist_keyword` |
| 9 | whitelist keywords — non-empty list requires ANY match (OR semantics, not AND) | `services.py:92-95` | `whitelist_mismatch` |
| 10 | legacy `regex_patterns` compat — only when `rx_block` AND `rx_require` are both empty | `services.py:102-111` | (routes to 11/12) |
| 11 | `regex_block` — ANY match | `services.py:114-123` | `regex_block:<pattern[:48]>` |
| 12 | `regex_require` — ANY match | `services.py:126-138` | `regex_mismatch` |
| 13 | fall-through | `services.py:140` | — (reason `filter_passed`) |

**Note on the docstring ordering claim**: the class docstring
(`services.py:40-44`) advertises "1. service messages 2. media type allow/deny 3. length
bounds 4. blacklist 5. whitelist 6. regex patterns". The code as written places
`block_links` (step 5) **between** the media gate and the length bounds, and splits the
media gate into block-then-allow. The Go port's docstring
(`filter.go:78-81`) copies the same stale five-step summary. The actual order is what
the table above records (verified by execution, see §7 probes).

### 2b. The 12 FilterRule settings

`FilterRule` dataclass at `entities.py:2312-2331`; persisted in `filter_rules` table
(`sqlite_repositories.py:961-968`, 14 columns).

| Feature | meaning | default | precedence | AND/OR | drop reason | legacy compat |
|---|---|---|---|---|---|---|
| FLT-001 `is_enabled` `entities.py:2325` | v30 master switch; off ⇒ filter skipped whole | `True` | step 2, before every other check | n/a | `filter_disabled` (ALLOW) | pre-v30 rows read as enabled (`sqlite_repositories.py:993-995`: "Never default a stored filter to off") |
| FLT-002 `drop_service_messages` `entities.py:2328` | drop Telegram service messages | `True` | step 0 | n/a | `service_message_dropped` | when no filter is attached at all, service messages are dropped unconditionally (`services.py:50-51`) — the attached-filter case honors the flag |
| FLT-003 `blocked_media_types` `entities.py:2327` | deny-list of media type strings | `[]` | step 3, BEFORE allow-list | OR | `media_blocked:<media>` | the literal value `"link"` doubles as a link sweep (step 3b) — undocumented in the entity |
| FLT-004 `allowed_media_types` `entities.py:2326` | allow-list; non-empty ⇒ "only these" | `[]` | step 4, AFTER block-list | membership | `media_not_allowed:<media>` | compared exact-then-lowercased (`services.py:73`); the row is a separate store from the task-level `allowed_media_types` column (`entities.py:407`, SET-020) |
| FLT-005 `block_links` | drop link-bearing text | `False` | step 5 | n/a | `link_blocked` | uses `URL_REGEX` OR substring fallbacks `http://`/`https://`/`t.me/` (`services.py:80`) |
| FLT-006 `min_message_length` `entities.py:2329` | minimum `len(text)` | `0` (falsy ⇒ disabled) | step 6 | n/a | `too_short` | `len()` is **characters of `effective_text`**; media messages with no caption read as `""` and are dropped (verified: probe P5) |
| FLT-007 `max_message_length` `entities.py:2330` | maximum `len(text)` | `0` (falsy ⇒ disabled) | step 7 | n/a | `too_long` | 0 disables — there is no "no limit" distinction other than 0 |
| FLT-008 `blacklist_keywords` `entities.py:2318` | forbidden words/phrases | `[]` | step 8 | OR (any) | `blacklist_keyword` | matched via `contains_word(word_boundary=False)` (`services.py:157`) — substring semantics preserved deliberately ("the fix must not change which messages an existing task blocks", `services.py:152-155`) |
| FLT-009 `whitelist_keywords` `entities.py:2317` | required words/phrases | `[]` | step 9 | OR (any) — empty list = no constraint | `whitelist_mismatch` | same matcher as FLT-008 |
| FLT-010 `regex_block` `entities.py:2320` | blocking patterns | `[]` | step 11 | OR (any) | `regex_block:<p[:48]>` | beats legacy |
| FLT-011 `regex_require` `entities.py:2321` | requirement patterns | `[]` | step 12 | OR (any) | `regex_mismatch` | beats legacy |
| FLT-012 `regex_patterns` `entities.py:2319` | legacy undirected list | `[]` | step 10, only when 010/011 both empty | routed to block-or-require | inherits 011's reason | **Deterministic compat rule** (`services.py:103-111`): blacklist set + no whitelist ⇒ blocking; otherwise ⇒ require-any. Go mirrors at `filter.go:171-177` |

**Regex fails CLOSED.** An un-compilable or hostile pattern is a DROP, never an allow:
- `regex_block`: `services.py:115-121` — logs `[FILTER_ENGINE:INVALID_REGEX]` "failing
  closed" and returns `invalid_regex_block:<pattern[:32]>`.
- `regex_require`: `services.py:128-135` — same, `invalid_regex_require:<pattern[:32]>`.
- The compiled object comes from `compile_user_regex` (`content_policy.py:352-362`),
  which refuses patterns > 1000 chars or with a nested-quantifier / ambiguous-alternation
  shape (`_hostile_pattern` `content_policy.py:312-314`, patterns at
  `content_policy.py:101` and `:108`) and always compiles with
  `re.IGNORECASE | re.MULTILINE` (`content_policy.py:362`).
- Matches run through `bounded_search` (`content_policy.py:317-330`) — wait: **FilterEngine
  does NOT use `bounded_search`**. It calls `compiled.search(text)` directly
  (`services.py:122`, `:137`). Only `ContentPolicy` applies the 8 KiB ceiling
  (`content_policy.py:883`, `:897-900`). This is an ReDoS-hardening **gap on the
  FilterRule path** (see FLT parity findings / unknowns).

### 2c. `_kw` — the keyword matcher delegated to the policy layer

`FilterEngine._kw` (`services.py:142-157`) delegates to `content_policy.contains_word`
with `word_boundary=False`, so a legacy FilterRule keyword list behaves exactly like the
same words on a content policy: multi-word phrases survive whitespace variation,
Persian/Arabic letter folding and case folding apply. Word boundaries stay off
deliberately (legacy substring behaviour, `services.py:152-155`).

---

## 3. ContentPolicy contract (POL-*)

`ContentPolicy` — `core/domain/content_policy.py:690-938`. Verified production call path
(FILTER stage):

```
DurableMessagePipeline.process_message
  → rule.policy_chain(source_chat_id=payload.chat_id)   (pipeline.py:974-977)
    → ForwardRule.content_policy          (entities.py:1359-1378)
      → content_policy_config             (entities.py:1098-1147)  — merges nested blob + flat keys + legacy aliases + _heal_regex
    → + source override                   (entities.py:1397-1401)
  → for policy in chain: policy.evaluate(payload.effective_text, m_type)  (pipeline.py:984-985)
     → ContentPolicy.evaluate             (content_policy.py:823-903)
```
Second call path (ENQUEUE stage, target-scoped only):
```
pipeline.py:1197  rule.policy_chain(source_chat_id=..., target_chat_id=target)
pipeline.py:1199-1205  only str(policy.source).startswith("target:") are re-decided
pipeline.py:1220-1229  chain[1:] transform (target overrides only)
```
Third call path (Go Worker, per-target rewrite): `transform.go:56-82`
`ApplyTargetOverride` — RemoveLinks + StripWords only, and the Go filter at
`delivery.go:212` does NOT re-evaluate any ContentPolicy (comment at
`delivery.go:193-199`: ingest-side content policies are "baked into the job payload").

### 3a. Master switch `enabled` (POL-001)

`ContentPolicy.enabled: Optional[bool] = None` (`content_policy.py:694`). Semantics via
`is_active` (`content_policy.py:791-797`):
- `enabled is False` ⇒ **off** (`:793-794`)
- `enabled is True` ⇒ active **only when** `has_rules` (`:795-796`)
- `enabled is None` (absent) ⇒ active when `has_rules` (`:797`)

So `None` and `True` behave identically. `has_rules` (`content_policy.py:776-788`) is
true when any of: blacklist/whitelist words, block_links, remove_links, strip_words,
replacements, regex_block, regex_require, min_length, max_length, allowed_media,
blocked_media. **Media allow/block lists count toward `has_rules`** — a policy with only
`allowed_media=["photo"]` is an active policy that can reject.

**Registry/entity mismatch (verified):** `rule_settings.py:240-242` registers
`content_policy_enabled` with default `False` and help "Master switch for this task's
content rules". The entity property `content_policy_enabled`
(`entities.py:1166-1171`) returns `bool(cfg.get("enabled"))` when the key is present and
**falls back to `self.content_policy.has_rules`** when absent. `apply_settings` writes
`False` on any task whose form posted the key — which turns a task whose policy was
active-by-`has_rules` into an explicitly-disabled one. Conversely writing `True` on a
policy with no rules is a no-op. The auto/None path is reachable only by never writing
the key. Severity: medium — a settings form round-trip can silently switch a task's
policy off.

### 3b. Per-list switches `blacklist_off` / `whitelist_off` (v30, POL-002)

`content_policy.py:697-698`; read from the stored blob with a missing key meaning "on"
(`content_policy.py:731-734`, "an absent switch must never disable a configured list
behind the owner's back"). Effects, verified in `evaluate`:
- `blacklist_off` skips the blacklist word gate (`content_policy.py:847`) **and** the
  `regex_block` loop (`content_policy.py:873-875`, `break` on the flag)
- `whitelist_off` skips the whitelist word gate (`content_policy.py:852`) **and** the
  `regex_require` gate (`content_policy.py:886`)
- The comment is explicit that switching one list off must not affect the other
  (`content_policy.py:844-846`), and that "regex_block is blacklist-side,
  regex_require is whitelist-side" (`content_policy.py:870-872`) — parking a list parks
  its patterns.
- `is_active` / `has_rules` do **not** consult the switches: a policy whose only rules
  are switched-off lists is still "active" and evaluates to
  `PolicyDecision(True, "policy_passed")` (verified by reading `evaluate`: every gate is
  guarded, so it falls through to `:903`).

### 3c. `whitelist_mode` — any / all / phrase (POL-003)

`content_policy.py:701`, `evaluate` at `content_policy.py:852-868`:

| mode | semantics | reason on failure | evidence |
|---|---|---|---|
| `any` (default) | at least one whitelist word matches | `whitelist_mismatch` | `:866-868` |
| `all` | every word must match; short-circuits when not even one matches | `whitelist_missing:<first 3 missing>` | `:853-861` — two-stage check: first `_first_matching_word` (if not one word is present, no need to check each), then the per-word `contains_word` sweep |
| `phrase` | the words joined by a single space, in order | `whitelist_phrase_missing` | `:862-865` |

Verified by execution (probe P9): words `["foo","bar"]` on text `"foo and baz"` in
`all` mode ⇒ `whitelist_missing:bar`; in `phrase` mode `"foo bar"` passes and
`"bar foo"` fails (order matters).

### 3d. Match flags `case_sensitive` / `word_boundary` / `normalize` (POL-004)

`content_policy.py:712-714`, defaults `False` / `True` / `True`. All three flow into
`_match_flags()` (`content_policy.py:816-821`) and are passed to `contains_word` /
`_first_matching_word`. Entity-side, `from_dict` treats absent `word_boundary` and
`normalize` as `True` and absent `case_sensitive` as `False`
(`content_policy.py:741-742`) — matching the dataclass defaults.

Flag effects (matcher internals):
- `normalize=True` folds Persian/Arabic letter variants via `_FOLD_MAP`
  (`content_policy.py:63-86`), strips diacritics/tatweel/zero-width & bidi marks via
  `_RE_INVISIBLE` (`content_policy.py:90-92`), and allow an invisible run between the
  keyword's characters (`_INVISIBLE_RUN` `content_policy.py:161`, used as the separator
  in `_needle_pattern` `content_policy.py:202`).
- `case_sensitive=False` compiles the needle pattern case-insensitively
  (`content_policy.py:214`).
- `word_boundary` selects the match mode (`_match_mode` `content_policy.py:164-172`):
  `substring` (no boundaries) when off; `prefix` when the needle opens with
  `#`/`@`/`$`/`/` (`_TOKEN_MARKERS` `content_policy.py:147`) — so `#ad` catches
  `#advert` and `@scam` catches `@scam_bot` (`content_policy.py:205-209`);
  `word` (with `(?<!\w)` left and `(?!\w)` right lookarounds) when the needle has an
  alphanumeric character; else `substring`.

### 3e. Regex lists, length bounds, media lists, block_links (POL-005)

`evaluate` order (cheapest first, `content_policy.py:824` — "lengths, media, links,
literal words, regexes"):

| # | check | evidence | reason on reject |
|---|---|---|---|
| 0 | `is_active` False ⇒ allow | `:825-826` | `policy_inactive` |
| 1 | `blocked_media` contains media | `:830-831` | `media_blocked:<media>` |
| 2 | `allowed_media` non-empty and media not a member | `:832-833` | `media_not_allowed:<media>` |
| 3 | `min_length` / `max_length` on `len(body)` | `:834-837` | `too_short:<n>` / `too_long:<n>` |
| 4 | `block_links` and `_RE_LINK.search(body)` | `:839-841` | `links_blocked` |
| 5 | blacklist words (unless `blacklist_off`) | `:847-850` | `blacklist:<word>` (+ `matched=(word,)`) |
| 6 | whitelist words (unless `whitelist_off`), mode-dependent | `:852-868` | see §3c |
| 7 | `regex_block` (skipped when `blacklist_off`) | `:873-884` | `regex_block:<p[:48]>` |
| 8 | `regex_require` (skipped when `whitelist_off`) | `:886-901` | `regex_mismatch` |
| 9 | fall-through | `:903` | — (`policy_passed`) |

Both regex lists **fail closed** on an un-compilable or hostile pattern
(`content_policy.py:876-882` and `:888-895`, logging
`[CONTENT_POLICY:INVALID_REGEX]`), and both run through `bounded_search` (the 8 KiB
ceiling, `content_policy.py:883` and `:897-900`).

`_RE_LINK` (`content_policy.py:96-99`) is deliberately broader than `URL_REGEX`:
`https?://`, `ftp://`, `www.`, `t.me/`, `telegram.me/`, `telegram.dog/`, `tg://`. So
the *policy* link gate catches `tg://` and `ftp://` while the *FilterRule* link gate
(`services.py:26-29`, `:80`) does not. Verified by execution (probe P2): a FilterRule
with `block_links=True` **allows** `"see tg://resolve?domain=x"`, while
`has_link` on the same text is `True` and a policy with `block_links` drops it. This is
a genuine cross-engine inconsistency (see parity findings).

### 3f. `transform()` (POL-006)

`content_policy.py:907-938`. Order is fixed: **remove_links → strip_words →
replacements**, each gated by `is_active` (`:909`). Returns `(text, notes)` where notes
is `{}` when nothing changed (`links_removed`/`words_stripped`/`replacements`).

- `remove_links` instantiates `FilterEngine` locally ("no cycle, one implementation",
  `content_policy.py:915`) and calls `remove_urls` (`services.py:159-169`): markdown
  hyperlink `[text](url)` collapses to its anchor text (`:163`), then raw URLs and
  t.me/telegram links are deleted (`:165`), then whitespace is tidied (`:167-169`).
  Verified (probe P7): `"click [this](https://a.example/x) now"` →
  `("click this now", {"links_removed": True})`.
- `strip_words` uses `_strip_word` (`content_policy.py:946-969`): the same
  `_needle_pattern` the matcher uses, `finditer` over the **original** text so spans are
  valid, non-overlapping spans cut, then `_tidy` (`content_policy.py:972-976`) collapses
  runs of spaces and leading spaces before newlines. Verified (probe P8):
  `"the cat sat on the dog"` with `strip_words=["cat","dog"]` →
  `("the sat on the", {"words_stripped": 2})`.
- `replacements` uses `_apply_replacements` (`content_policy.py:979-1007`): all pairs
  compiled into ONE combined alternation and applied in a single pass over the original
  text (`:989-999`), so `a=>b` then `b=>c` yields `b`, not `c` — "a replacement value
  can never be re-scanned by a later rule" (`content_policy.py:35-36`). A `re:` prefix
  marks the find side as a regex (`:992`). Note: the *paired* registry entries with
  `mode`/`flags`/`name` (`RS:104-109`) are the Engine-4/`text_replace` shape
  (`services.py:239-245` delegates to `apply_replacements`) — the policy blob's flat
  `re:` prefix is the older shape and the two stores are not interoperable (documented
  here as a boundary; RPL/B owns the detail).

**One flag, three readers (POL-006 finding):** `remove_links` is (a) a first-class
column `entities.py:402` read by the LME gate `pipeline.py:1448` (D-owned), (b) folded
into the policy blob at `entities.py:1366-1367` and applied by
`content_policy.py:914-920`, and (c) part of the cleaner flag set
`entities.py:940-943` / `CLEANER_OPTION_FLAGS` `entities.py:955`. All three can fire on
one message; remove_urls is idempotent, so the outcome converges, but the log lines and
`evaluation.links_removed` bookkeeping are multiplied.

### 3g. `policy_chain()` (POL-007)

`entities.py:1380-1407`. Builds `[task_policy, source_override?, target_override?]`:
- task policy first (`entities.py:1394`), built by `content_policy` (`:1359-1378`)
- at most one source override, matched on `_chat_key(key) == _chat_key(source_chat_id)`
  (`entities.py:1397-1401`), first match wins, `break`
- at most one target override, same (`entities.py:1402-1406`)
- overrides are **additive constraints**: "every policy in the chain must allow the
  message... Nothing in a chain can relax the task's own rules"
  (`entities.py:1095-1097`); the FILTER stage loops the chain and returns on the first
  non-allow (`pipeline.py:984-1004`).
- `_chat_key` normalization (documented at `entities.py:1388-1390`) is what makes a UI
  sending `-100123` and a client sending it as a number the same override.
- `ForwardRule.content_policy` also folds the first-class `block_links` flag
  (`entities.py:1364-1365`) and `remove_links` (`:1366-1367`) into the blob, and the
  pre-policy `metadata["blacklist"]`/`["whitelist"]` lists when the blob has none
  (`:1372-1377`) — so a task configured before the content policy existed "is still
  filtered instead of silently forwarding everything it used to block".

### 3h. Matcher helpers (all in `content_policy.py`)

| helper | line | purpose |
|---|---|---|
| `fold_text` | `131-142` | NFC → strip invisibles → translate `_FOLD_MAP` → casefold. Comparison form of a string. |
| `contains_word` | `285-309` | the canonical keyword matcher; compiles the needle into a variant-aware pattern run against the **original** text (folding the haystack "moves character offsets... and offsets are exactly what `strip_words` needs", `content_policy.py:181-186`) |
| `_needle_pattern` | `175-214` | `lru_cache(4096)` keyword → regex; a keyword that folds away matches nothing (`(?!`)`, `:191`); multi-word needles get `\s+` (`:196-199`) |
| `_folded_needles` / `_folded_body` / `_first_matching_word` | `217-282` | the cheap pre-filter: `needle in folded_body` skips words that cannot match at one C-level scan each, "never... what the decision is" (`content_policy.py:32-35`) |
| `compile_user_pattern` | `333-349` | `lru_cache(2048)`, length cap 1000, hostile-shape refusal, caller-chosen flags |
| `compile_user_regex` | `352-362` | the content-filter spelling: `re.IGNORECASE | re.MULTILINE`; "Shared by every stage that evaluates a pattern a user typed (the content policy and `FilterEngine`)" (`:356-360`) |
| `bounded_search` | `317-330` | match only `text[:8192]` (verified go parity: `filter.go:259-264`) |
| `_hostile_pattern` | `312-314` | `_REDOS` (nested quantifier) **or** `_REDOS_ALT` (ambiguous alternation under a quantifier, `content_policy.py:105-108`: "(a|a)*b is exponential too") |
| `has_link` | `121-128` | `_RE_LINK` (broader than `URL_REGEX`) |
| `split_word_list` | `599-637` | one canonical list parser: ASCII comma, Persian/Arabic comma `\u060c`, newlines; duplicates dropped, first wins; an entry is an entry when the input is already a list ("a phrase that contains a comma stays whole", `:613-617`) |

---

## 4. Blacklist/whitelist distinct implementations

Six distinct stores were confirmed. **Two of the six cannot reject a message.**

| # | store | where stored | what reads it | can reject? | evidence |
|---|---|---|---|---|---|
| 1 | `FilterRule.blacklist_keywords` / `whitelist_keywords` | `filter_rules` table columns (`sqlite_repositories.py:961`) | `FilterEngine.evaluate` steps 8-9, via `_kw` (`services.py:89-95`) | **YES** — `blacklist_keyword` / `whitelist_mismatch` | `entities.py:2317-2318` |
| 2 | FilterRule `regex_block`/`regex_require` (+ legacy `regex_patterns`) | `filter_rules` table | `FilterEngine.evaluate` steps 10-12 (`services.py:98-138`) | **YES** | `entities.py:2319-2321` |
| 3 | `ForwardRule.blacklist` / `whitelist` (alias of the policy blob) | `metadata["content_policy"]["blacklist_words"]`; legacy `metadata["blacklist"]` folded on read | `ContentPolicy.evaluate` gate 5/6 (`content_policy.py:847-868`) — the compat properties write through to the policy and pop the legacy key (`entities.py:1460-1464`) | **YES** (through the policy) | `entities.py:1451-1480` |
| 4 | `ForwardRule.blacklist_users` / `whitelist_users` | `metadata["blacklist_users"]` / `metadata["whitelist_users"]` | **NOTHING in the decision path.** Verified: `grep` over `core/application/pipeline.py`, `core/domain/services.py`, `core/application/use_cases.py` returns zero reads of these properties; only the bot UI counts them for display (`pro_bot_ui.py:3085,8044-8057`). `MessagePayload.sender_id` exists (`entities.py:2470`) and is used for header placeholders (`services.py:370`) but is never compared against these lists. | **NO** — STORED_BUT_UNUSED for filtering | `entities.py:1483-1506` |
| 5 | `ContentPolicy.blacklist_words` / `whitelist_words` (+ `regex_block`/`regex_require`) | task's `metadata["content_policy"]` blob, or `source_policies[key]` / `target_policies[key]` override blobs | `ContentPolicy.evaluate` via `policy_chain` in the FILTER stage and (target-scoped) the ENQUEUE stage | **YES** | `entities.py:1198-1227`, `pipeline.py:974-1004`, `pipeline.py:1197-1229` |
| 6 | source/target policy overrides | `metadata["content_policy"]["source_policies"|"target_policies"]` | same as 5, one extra chain entry per direction | **YES, additive-only** — "an override is an *additional* constraint, never a relaxation" (`content_policy.py:17-19`, `entities.py:1095-1097`) | `entities.py:1247-1262`, `entities.py:1397-1406` |

Additional cross-references requested:
- **Entity `blacklist_words`/`whitelist_words` (`entities.py:1198/1206`) vs the policy
  blob**: they ARE the policy blob — `_policy_key("blacklist_words")` reads
  `content_policy_config` (`entities.py:1155-1158`), which merges the nested blob, flat
  metadata keys (`entities.py:1124-1128`) and legacy aliases
  (`entities.py:1131-1138`) — three spellings of one list, one authoritative home.
- **A seventh, softer list:** `ForwardRule.whitelist` is ALSO read by the keyword-reply
  trigger `classify_keyword_trigger` (`services.py:1514-1517`) with raw
  `kw in haystack` substring semantics — NOT `contains_word`, NOT the policy. A word
  that fails the policy matcher (e.g. typed with Arabic letter variants) can still fire
  the reply, and vice versa. This is the keyword-reply path (C-owned KW-*); flagged here
  because it is the one place a "whitelist" is read with a third, weaker matcher.

---

## 5. Media filtering (MED-*)

`core/domain/media_filter.py` — the single shared gate, "every forwarding path applies"
(`media_filter.py:1-14`, whose docstring explicitly says the inline version "made the
two paths disagree... One decision function is what stops that drift").

### 5a. `MEDIA_TYPES` (20 entries) — `media_filter.py:30-51`

`text`, `photo`, `video`, `video_note`, `document`, `audio`, `voice`, `animation`,
`sticker`, `poll`, `location`, `contact`, `venue`, `dice`, `game`, `invoice`, `story`,
`giveaway`, `service`, `other`. 20 values confirmed by count.

### 5b. The 5 `_BLOCK_FLAGS` — `media_filter.py:56-62`

| flag | covers | reason code |
|---|---|---|
| `block_voice` | `("voice", "audio")` | `voice_blocked` |
| `block_audio` | `("audio", "voice")` | `audio_blocked` |
| `block_video` | `("video", "video_note")` | `video_blocked` |
| `block_stickers` | `("sticker",)` | `sticker_blocked` |
| `block_text` | `("text",)` | `text_blocked` |

Comment (`media_filter.py:53-55`): "A voice note is audio and a round video is a video:
the owner ticked a category, not a Telegram enum". Note `block_voice` and `block_audio`
cover the identical pair `{voice, audio}` — either flag blocks both, so the two toggles
in the UI are semantically redundant (verified by reading the tuples; both contain both
types). `photo`, `document`, `animation`, `poll`, `location`, `contact`, `venue`, `dice`,
`game`, `invoice`, `story`, `giveaway`, `service`, `other` have **no block flag** — the
only way to block them is the allow-list.

### 5c. Precedence and `link_block_reason`

`media_block_reason` (`media_filter.py:77-91`):
1. **allow-list first** ("forward only photos" is a stronger statement than "don't
   forward video", `media_filter.py:80-83`): if `allowed_media_types` is non-empty and
   the type is not in it ⇒ `media_not_allowed:<type>` — this beats every block flag.
2. then the block flags in `_BLOCK_FLAGS` order (`media_filter.py:88-90`).
- `_allowed_types` tolerates a hand-edited string row: comma/newline separated
  (`media_filter.py:72-73`), and normalises each entry (`media_filter.py:65-67`).
`link_block_reason` (`media_filter.py:94-97`): `block_links` AND `has_link(text)` ⇒
`link_blocked`. Because it uses `has_link`, it catches `tg://`, `ftp://`,
`telegram.me/`, `telegram.dog/` — a strictly broader net than the FilterRule gate's
`URL_REGEX`+substring trio (probe P2).

### 5d. Production call path

```
pipeline.py:955  m_type = normalise_media_type(payload.media_type)
pipeline.py:956  media_reason = media_block_reason(rule, m_type) or link_block_reason(rule, payload.effective_text)
pipeline.py:959-966  drop with the reason code, counted as filtered
```
Also called from `SmartRoutingEngine.evaluate_rule` (`services.py:552-554`) — "the shared
decision, so the rule preview shown by the bot and the gRPC test endpoint cannot
disagree with the engine" (`services.py:550-551`), i.e. the routing matcher applies the
task-level media gate as part of matching. `SmartRoutingEngine` additionally applies
`media_handling` TEXT_ONLY/MEDIA_ONLY (`services.py:556-560`), which is a *separate*
gate not in `media_filter.py`.

### 5e. FilterRule vs ForwardRule media lists (MED-004)

Two independent stores:
- `FilterRule.allowed_media_types` / `blocked_media_types` — `entities.py:2326-2327`,
  read by `FilterEngine.evaluate` steps 3-4 (`services.py:64-74`) against
  `payload.media_type`. Attached standalone filter object only.
- `ForwardRule.allowed_media_types` — first-class **column** `entities.py:407`
  (registered at `RS:137` with the comment that backups and the REST contract already
  carry it, `RS:133-136`), read by `media_filter._allowed_types` (`media_filter.py:70-74`).
There is **no** `ForwardRule.blocked_media_types` — the task-level deny side is the six
block flags (`SET-014..019`). So the same-named list on a task and on its attached
filter are different gates evaluated at different points (attached filter step 3/4 in
`FilterEngine.evaluate` at `pipeline.py:918` vs task flags at `pipeline.py:956`) and can
disagree: a task with `allowed_media_types=["photo"]` and an attached filter with
`allowed_media_types=["video"]` drops everything that is not both.

---

## 6. Rule matching / SmartRoutingEngine (RT-*)

`core/domain/services.py:415-655`. Verified production call path:

```
DurableMessagePipeline.process_message
  → active_rules = self._rules.list_active_by_source(payload.chat_id)  (pipeline.py:688)
  → matching = self._routing.matching_rules(payload, active_rules)     (pipeline.py:689)
    → RoutingPolicy.matching_rules (services.py:665-666) → SmartRoutingEngine.matching_rules (services.py:622-655)
      → evaluate_rule (services.py:523-574)
        → matches_source (services.py:536-537; entities.py:1967-1996)
        → is_vip_origin (services.py:419-498)
        → media_block_reason (services.py:552)
        → media_handling (services.py:556-560)
        → check_loop for every target (services.py:566-572)
```
Second path: `core/application/use_cases.py:840-844` (the non-durable path,
`PipelineOutcome(False, "no_matching_rules")`). Preview surfaces:
`pro_bot_ui.py:12820-12823` (the bot's rule simulator) and `grpc_server/servicers.py:1490`
(the gRPC `TestRule` endpoint, which also evaluates the content policy for simulation
fidelity `servicers.py:1495-1502`).

### 6a. `message_category` (RT-001)

`ForwardRule.message_category` field, `entities.py:229` default `"ALL"`. Documented
values `ALL | VIP | REGULAR | CHANNEL_POST | USER_MESSAGE` (`entities.py:229` docstring
in the field comment). `evaluate_rule` only handles two branches
(`services.py:540-548`): `("VIP", "VIP_ONLY")` → require `is_vip_origin`, and
`("REGULAR", "NORMAL", "NORMAL_ONLY")` → require NOT `is_vip_origin`. **`CHANNEL_POST`
and `USER_MESSAGE` are not implemented** — they fall through to the ALL branch
(verified by reading `services.py:539-548`; no other branch exists). Status:
`PARTIALLY_IMPLEMENTED` for those two categories (UI offers them, the matcher ignores
them).

### 6b. `is_vip_origin` (RT-002)

`services.py:419-498`. Reads `rule.detection_criteria` (with `match_mode` ANY/ALL,
`services.py:430`):
- `forward_origin_chat_ids` / alias `origin_chat_ids` (`services.py:432`)
- `forward_origin_titles` / alias `origin_titles` (`services.py:433`)
- `text_contains` / alias `keywords` (`services.py:434`)
- `regex_pattern` / alias `text_regex` (`services.py:435`)
- `require_forward_origin` (`services.py:441`)
Strict rules (from the docstring `services.py:421-428`): never assume a regular message
is VIP; absent forward origin is `UNKNOWN_ORIGIN`, not a guess. When **no** criteria are
configured at all, any message with a forward origin is VIP
(`services.py:485-489`: `forward_chat_id_match:<id>`), else `UNKNOWN_ORIGIN`.
- ID matching compares `from_chat_id` exactly OR username (case-folded, `@`-stripped)
  against the configured ids (`services.py:452-455`); title matching is a **substring**
  test in this engine (`clean_title in fwd_title`, `services.py:456`) — note the
  branding engine (`branding.go:27-31`) tests membership **in both directions**
  ("Python reference tests `wanted in title or title in wanted`") — those are two
  different substring/VIP engines with two different title semantics.
- Text keywords here are a raw lowercase `kw in eff_text` substring scan
  (`services.py:464-468`) — NOT `contains_word`. A third matcher again.
- Regex uses `re.search(..., re.IGNORECASE)` with a bare `except re.error`
  (`services.py:473-482`): in ANY mode an invalid pattern is simply not a match; in ALL
  mode it rejects with `invalid_regex`. **No ReDoS guard, no length cap** — unlike
  `compile_user_regex`. The VIP regex is stored in `detection_criteria` and does not go
  through the registry's `regex_block` validation.

### 6c. `detection_criteria` / match flags on the entity (RT-003)

`ForwardRule.detection_criteria` field (`entities.py:230`); entity properties:
`source_chat_ids` `entities.py:1873`, `source_chat_names` `:1901`,
`forward_origin_chat_ids` `:1917`, `forward_origin_titles` `:1923`,
`text_contains` `:1929`, `regex_pattern` `:1935`, `match_mode` `:1941` (default `"ANY"`,
uppercased). `matches_source` `entities.py:1967-1996` checks `source_chat_id`, every
`source_chat_ids` entry, and `source_chat_name`, comparing via `_chat_key` equality OR
username case-folded equality (`entities.py:1982-1987`), and returns False when the
task is inactive (`entities.py:1976-1977`). The extra sources are also what the SQL
candidate query selects on (`sqlite_repositories.py:788`, `postgres_repositories.py:554`,
`database.py:1574`).

### 6d. `check_loop` / `is_loop` (RT-004)

`services.py:500-521`. Three conditions: target == source (by id or by username,
`:506`); target == the message's forward origin (`:510-513`); target == the
intermediate channel while the message originated there (`:516-519`).
`evaluate_rule` applies it **per fan-out target** (`services.py:566-572`): the rule is
rejected only when **every** target would loop — "a single looping target is skipped by
the fan-out... which would otherwise silently drop the other, legitimate targets"
(`services.py:562-565`). `RoutingPolicy.is_loop` (`services.py:668-675`) exposes the
per-target check, and `pipeline.py:1168-1182` skips the looping target at enqueue with
a `[PIPELINE:LOOP_SKIP]` line and deliberately does **not** count it as filtered
(`pipeline.py:1172-1174`).
The gRPC TestRule endpoint reproduces the same every-target semantics
(`servicers.py:1505-1512`, with the comment that checking the primary alone "made the
dry-run contradict the dispatcher").

### 6e. `matching_rules` — priority, execution order, per-owner single-match (RT-005)

`services.py:622-655`:
- sort key `(-priority, execution_order, created_at)` (`services.py:636-639`); default
  `priority=10` (`entities.py:226`), `execution_order=0`.
- **single-match is enforced per owner** (`services.py:643-653`): a task that matches
  and does not have `multi_route` consumes the message for its tenant; "a task never
  consumes a message on behalf of another tenant" (`services.py:628-630`), which is what
  stops one tenant's sort key from starving another's (`services.py:631-634`).
- `multi_route=True` (`entities.py:228`) lets a rule match without consuming, so later
  rules also run.
- `evaluate_rule` gates: `is_active` (`:525-526`), paused (`:529-534` — note
  `is_currently_paused()` is the expiry-aware variant, `entities.py:351-356`),
  `matches_source`, category, media, media_handling, loop.
- Trigger-event filtering happens after matching, in the pipeline
  (`pipeline.py:690`: `valid_rules = [r for r in matching if trigger in r.trigger_events]`).

### 6f. `decide_route` (RT-006)

`services.py:605-620` — `evaluate_rule` then `should_brand_hop`
(`services.py:576-589`, which delegates to `branding.should_hop` with VIP context from
`vip_criteria_configured` `services.py:591-603`), then maps `forward_mode` to
`RoutingDecision` (BRANDING_VIA_C / DIRECT_FORWARD / CUSTOM_HEADER_COPY / DIRECT_COPY).

**Go parity for RT:** the Worker does **not** re-run routing at all — jobs are enqueued
per matched target by the Python pipeline; the Worker's `rule.ForwardRule`
(`rule.go:22-77`) carries `MessageCategory` and `DetectionCriteria` only for the
branding hop decision (`branding.go`), not for matching. `SmartRoutingEngine` has no Go
port (searched: no `matching_rules`/`is_vip_origin` equivalent in `worker/`). This is
correct for the strangler-fig split (routing is ingest-side) — noted as ABSENT-by-design,
not a gap.

---

## 7. Go parity

Read in full: `worker/internal/engine/filter/filter.go` (482 lines),
`worker/internal/engine/transform/transform.go` (153 lines),
`worker/internal/engine/delivery/delivery.go:180-320`,
`worker/internal/domain/rule/rule.go`, `worker/internal/engine/filter/filter_test.go`.

### 7a. What the Go filter does vs Python `FilterEngine.evaluate`

`filter.go:84-222` `Engine.Evaluate(text, mediaType, isService, f)`. The Go struct
carries all 13 FilterRule fields (`rule.go:88-101`). Checkout order vs the Python table
in §2a:

| step | Python | Go | agreement |
|---|---|---|---|
| service messages | `services.py:47-51` (drop when no filter OR `drop_service_messages`) | `filter.go:85-94` (`f == nil || f.DropService` → `service_message_dropped`) | **EXACT** — Go collapses the two Python branches into one condition that is logically identical |
| disabled filter | `services.py:59-60` | `filter.go:101-103` | EXACT (`filter_disabled`) |
| blocked media | `services.py:64-67` | `filter.go:106-115` | EXACT (`media_blocked:<media>`) |
| blocked-"link" sweep | `services.py:68-71` | `filter.go:116-122` | EXACT (same 4-way URL/substring test) |
| allowed media | `services.py:73-74` | `filter.go:125-136` | EXACT (`media_not_allowed:<media>`) |
| min/max length | `services.py:83-87` | `filter.go:138-143` | EXACT (`too_short`/`too_long`) |
| blacklist | `services.py:89-90` via `contains_word(word_boundary=False)` | `filter.go:145-149` via `containsWord(text, kw, false)` | EXACT (matcher ported, see below) |
| whitelist | `services.py:92-95` | `filter.go:150-161` | EXACT (`whitelist_mismatch`) |
| legacy regex compat | `services.py:102-111` | `filter.go:171-177` | EXACT — same deterministic rule, same comment ("Changing this rule changes which messages decade-old tasks forward", `filter.go:169-170`) |
| regex_block / regex_require | `services.py:114-138` | `filter.go:179-210` | EXACT, including **fail closed** (`filter.go:180-185`, `:194-197`) and reason strings |
| `block_links` | `services.py:79-81` — **step 5, before length** | `filter.go:213-219` — **step 11, AFTER the regex lists** | **ORDER MISMATCH** — see P-ORD |

### 7b. Matcher parity (Go lines 266-482)

- `variantClass` (`filter.go:272-278`) mirrors `_VARIANT_CLASS` (`content_policy.py:151-157`)
  exactly, including the same five letters and the same accepted variants.
- `invisibleRun` (`filter.go:282`) is byte-identical to `_INVISIBLE_RUN`
  (`content_policy.py:161`); `invisibleRe` (`filter.go:464`) to `_RE_INVISIBLE`
  (`content_policy.py:90`).
- `matchMode` (`filter.go:312-328`) mirrors `_match_mode`
  (`content_policy.py:164-172`) — token markers first (`filter.go:316-321`), then
  alnum ⇒ `word` (`filter.go:322-326`).
- `needlePattern` (`filter.go:333-384`): same construction — folded + whitespace
  collapsed needle, per-char variant classes, `\s+` for spaces
  (`filter.go:343-346`), invisible runs between elements via `joinInvisible`
  (`filter.go:393-405`). **One documented deliberate difference:** "RE2 (Go's regexp)
  has no lookbehind, so the left word boundary is expressed as `(?:^|[^\w])`" and "the
  matched span therefore includes that preceding character; for a pure *decision* ...
  that is irrelevant" (`filter.go:359-363`). A decision cannot differ because of it.
- `foldText` (`filter.go:454-461`) = `foldText(normalize=True, case_sensitive=False)`
  (`content_policy.py:131-142`), using `norm.NFC` and `strings.ToLower`. Variant folding
  happens at needle-pattern build time in both (`filter.go:450-453` mirrors
  `content_policy.py:450-453` comment at `content_policy.py:180-186`).
- `collapseWS` (`filter.go:467-482`) turns every whitespace run into one space — the
  Go-side equivalent of the Python `_folded_*` whitespace collapse
  (`content_policy.py:188`, `:249`, `:260`).
- `compileUserRegex` (`filter.go:243-255`): 1000-char cap, both ReDoS guards
  (`reDoSNested`/`reDoSAlt` at `filter.go:56-57`, identical source patterns to
  `content_policy.py:101`/`108`), `(?im)` flags = `re.IGNORECASE | re.MULTILINE`
  (`filter.go:250`). Refusal fails closed (`filter.go:54-55`).
- `boundedMatch` (`filter.go:259-264`) truncates to `maxMatchLen=8192` — matching
  Python's `bounded_search` (`content_policy.py:317-330`). **But remember: the Python
  `FilterEngine.evaluate` does not call `bounded_search`** (`services.py:122/137` call
  `compiled.search` directly) while the Go filter always bounds. Go is *safer* here.

### 7c. What the Go filter does NOT do

- **No ContentPolicy at delivery.** "`delivery.go:193-199`" — the comment block at
  `delivery.go:193-199` states the pipeline's content policies "ran on the ingest side
  and their decision is baked into the job payload" and only the persisted FilterRule
  is re-evaluated (`delivery.go:212-221`). Confirmed by the filter package containing no
  policy type and by `transform.go:42-49` projecting an override to
  `{RemoveLinks, StripWords}` only.
- **Reason-string truncation is byte-based in Go, character-based in Python.**
  `filter.go:233-238` `truncate(s, 32)` slices bytes; Python slices characters
  (`services.py:121`, `:135`, `content_policy.py:882`). A multi-byte pattern (any
  non-ASCII regex, e.g. a Persian pattern — see `fixtures/14_persian_variants.json`)
  truncates at a different point and can even split a UTF-8 sequence in Go. Reason
  strings in logs/metrics will differ between engines for the same non-ASCII pattern.
- **Filter ownership guard is Python-only.** The pipeline refuses to use a filter owned
  by another tenant (`pipeline.py:903-916`); the Go `rule.Store.GetByID`
  (`rule.go:109`) fetches by id with no ownership check. The rule id comes from the
  task's own row, so the risk is a task whose row was edited to point at a foreign
  filter between enqueue and delivery.
- **`transform.go` vs `ContentPolicy.transform`** (per-target override):
  - RemoveLinks: Go `urlRegex.ReplaceAllString` (`transform.go:62-66`) removes raw
    URLs **including @usernames** (Go's `urlRegex` is Python's `URL_REGEX`, which has
    an `@[A-Za-z0-9_]{3,32}` alternative, `transform.go:26`) but does **not** collapse
    markdown hyperlinks, while Python's `remove_urls`
    (`services.py:159-169`) keeps the anchor text of `[x](url)` and does **not** strip
    usernames. Same flag, different output text. Verified by reading both
    implementations; Parity finding P-LNK.
  - StripWords: Go `strings.ReplaceAll` (`transform.go:74`) is a literal substring
    delete with no variant folding, no word boundaries, no whitespace-run tolerance;
    Python `_strip_word` (`content_policy.py:946-969`) is the full matcher. P-STRP.
  - Go `Tidy` (`transform.go:86-97`) collapses 3+ newlines and trims; Python `_tidy`
    (`content_policy.py:972-976`) collapses `[ \t]{2,}` and `[ \t]+\n` but **does not**
    collapse blank-line runs. Different post-condition.

### 7d. Existing parity infrastructure

`worker/internal/parity/` — 17 JSON fixtures (`01_blacklist_keyword.json` …
`17_no_filter_allows_service_when_drop_off.json`), `reference_filter.py` runs the real
`FilterEngine` over them, `parity_test.go` runs the Go engine on the same fixtures, and
`scripts/regen_parity_reference.sh` regenerates (comment in
`reference_filter.py:1-14`: "CI diffs it, so a Go port that drifts fails the build"). A
branding equivalent exists (`reference_branding.py`, `branding_parity_test.go`). The
fixture set covers: keyword/whitelist, regex both ways, legacy both ways, disabled
filter, service message, length bounds, media both ways, Persian variants, multi-word
phrase whitespace, link blocked, no-filter service pass-through.
**Coverage gaps in the fixtures** (verified by reading all 17): no fixture for
`block_links` on the FilterRule field itself (fixture 16 uses
`blocked_media_types:["link"]`), none for the block_links-vs-regex **order** difference,
none for non-ASCII pattern truncation of reason strings, none for the disabled-filter +
service-message interaction, none for `min_message_length` on a media message with no
caption (P5 above), none for the `bounded_search` asymmetry.

---

## 8. Parity findings

Each finding: fixture · Python output · Go output · expected · severity · recommended
separate fix. All Python outputs were produced by executing the real engines on
baseline `35ee530`; Go outputs by executing the real `filter.Engine` from
`worker/internal/engine/filter` (probe harness run under `worker/`, then removed).

### P-ORD — `block_links` is evaluated at a different position in the two engines (HIGH)

- **Fixture:** `filter = {is_enabled: true, block_links: true, regex_block: ["x{1,5}"]}`;
  text `"hello https://example.com world"`, media `text`, not service.
- **Python:** `DROP` reason `link_blocked` — `block_links` is step 5
  (`services.py:79-81`), before the regex gates.
- **Go:** `DROP` reason `regex_block:x{1,5}` — `block_links` is step 11
  (`filter.go:213-219`), after the regex gates.
- **Expected:** identical decision AND reason, since the reason names the gate that
  decided and operators debug from it.
- **Severity:** HIGH. Both drop, but the *reason* is what an operator reads, what
  `delivery.go:221` writes into the job's failure record (`"Filter: "+res.Reason`),
  and what the parity fixtures pin. A task whose filter is inspected via a Go-side drop
  log looks like it was blocked by a regex it was actually blocked by a link gate for.
- **Recommended fix (separate PR):** move the `filter.go:213-219` block_links gate to
  immediately after the allowed-media check (matching `services.py:73-81`), and add a
  parity fixture `18_block_links_gate_order.json` with both `block_links` and
  `regex_block` set.

### P-LNK — link-*stripping* output differs between `ContentPolicy.transform` and `transform.ApplyTargetOverride` (HIGH)

- **Fixture:** policy target override `{remove_links: true}`; text
  `"click [this](https://a.example/x) now dm @bob"`.
- **Python:** `("click this now dm @bob", {"links_removed": True})` —
  `ContentPolicy.transform` → `FilterEngine.remove_urls`
  (`content_policy.py:914-920` → `services.py:159-169`): markdown anchors are unwrapped
  and usernames are kept.
- **Go:** `"click  now dm "` — `transform.go:62-66` deletes the whole
  `[this](https://a.example/x)` (no anchor unwrapping) **and** deletes `@bob` because
  Go's `urlRegex` includes the `@[A-Za-z0-9_]{3,32}` alternative
  (`transform.go:26`, `:50`).
- **Expected:** the same delivered text. A target whose override strips links gets
  different content depending on which engine claimed the job.
- **Severity:** HIGH — visible content divergence on every message with a markdown link
  or a @mention under a target override with `remove_links`.
- **Recommended fix (separate PR):** port `remove_urls` (markdown unwrap, no username
  stripping) into `transform.go` for the override path, and add a parity fixture. The
  reverse-ported `HasLink` (`transform.go:118-124`) already shares the 4-way test with
  the Python gate, so the predicate is not the problem — only the rewrite.

### P-STRP — `strip_words` is variant/boundary-aware in Python and literal in Go (MEDIUM)

- **Fixture:** target override `{strip_words: ["cat"]}`; text `"the concatenate sat"`
  or Persian `"سلام کوچک"`.
- **Python:** `_strip_word` (`content_policy.py:946-969`) uses `_needle_pattern` with
  the task's match flags: `word_boundary` default True (so `cat` does NOT match
  `concatenate`) and Persian letter-variant classes (so `کوچک` matches the Arabic-kaf
  spelling). Output: `"the concatenate sat"` unchanged for the English case (no match),
  and a correct span deletion for the Persian case.
- **Go:** `strings.ReplaceAll` (`transform.go:74`) deletes the substring `cat` from
  `concatenate` → `"the  ate sat"`, has no notion of word boundaries, and no Arabic
  letter folding.
- **Expected:** word stripping that respects the same matching rules the *decision*
  side applies — otherwise a task strips a word in one engine and mangles a longer word
  in the other.
- **Severity:** MEDIUM (content divergence, but only on the target-override path with
  `strip_words` configured; word-boundary divergence also makes Go **delete more**
  than Python, which is data loss rather than just a different shape).
- **Recommended fix (separate PR):** expose the Go `needlePattern`
  (`filter.go:333-384`) for span extraction (the ported comment already says decision
  use is what made the lookbehind trade-off acceptable, `filter.go:359-363` — span
  removal needs the exact spans), and use `finditer`-equivalent spans in
  `ApplyTargetOverride`. Add a fixture.

### P-TRUNC — reason-string truncation is byte-slicing in Go and character-slicing in Python (LOW)

- **Fixture:** `filter = {regex_block: ["(" + "ب"*40]}` (any non-ASCII pattern longer
  than 32 units).
- **Python:** `invalid_regex_block:<first 32 characters>` (`services.py:121`).
- **Go:** `invalid_regex_block:<first 32 BYTES>` (`filter.go:184` →
  `truncate` `filter.go:233-238`) — for Persian/Arabic text that is ~10-16 characters
  and can end mid-codepoint (Go `s[:n]` on a string slices bytes; a mid-codepoint cut
  yields invalid UTF-8 in the reason string that is then stored in the job's failure
  record by `delivery.go:221`).
- **Expected:** identical reason vocabulary ("mirroring the Python engine's reason
  strings one-for-one", `filter.go:36-38`).
- **Severity:** LOW (diagnostics only) — but it is a real invalid-UTF-8-in-a-stored-
  field risk on the Go side.
- **Recommended fix (separate PR):** truncate on `[]rune` in `filter.go:233-238`, and
  add a parity fixture asserting the reason string equality for a non-ASCII pattern.

### P-BOUNDED — Python `FilterEngine.evaluate` skips the ReDoS text ceiling that Go and `ContentPolicy` apply (MEDIUM)

- **Evidence:** Python filter matches with bare `compiled.search(text)`
  (`services.py:122`, `services.py:137`); `ContentPolicy` uses `bounded_search`
  (`content_policy.py:883`, `:897-900`); Go uses `boundedMatch`
  (`filter.go:259-264`). The guard's own docstring says "Matching against the first
  `_MAX_MATCH_LEN` characters keeps the worst case linear" (`content_policy.py:113-118`).
- **Effect:** a FilterRule regex on a long message (a 4096-char message is the Telegram
  cap, but payloads here can be longer — e.g. an album caption concatenation) is
  unbounded in Python and bounded to 8 KiB in Go. A pattern that matches only beyond
  byte 8192 drops the message in Python and allows it in Go.
- **Expected:** the same guard on every user-pattern evaluation, per
  `compile_user_regex`'s claim that it is "Shared by every stage that evaluates a
  pattern a user typed (the content policy and `FilterEngine`)"
  (`content_policy.py:356-360`) — the sharing stops one call short.
- **Severity:** MEDIUM (both a ReDoS exposure on the Python ingest path and a decision
  divergence on long texts).
- **Recommended fix (separate PR):** route `services.py:122` and `:137` through
  `bounded_search`, plus a fixture with a >8 KiB text and a pattern that matches only
  in the tail.

### P-LINKGATE — three different "is this a link" predicates across the Python codebase (MEDIUM)

- **Fixture:** text `"see tg://resolve?domain=x"`.
  - `FilterEngine` `block_links` (`services.py:79-81`): **ALLOW** — `URL_REGEX`
    (`services.py:26-29`) has no `tg://` alternative, and the substring fallbacks are
    `http://`/`https://`/`t.me/` only. (Executed: probe P2.)
  - `media_filter.link_block_reason` (`media_filter.py:94-97`) via `has_link`
    (`content_policy.py:121-128`): **`link_blocked`** — `_RE_LINK` includes `tg://`,
    `ftp://`, `telegram.me/`, `telegram.dog/` (`content_policy.py:96-99`).
  - `ContentPolicy.block_links` (`content_policy.py:839-841`): **`links_blocked`**
    — same `_RE_LINK`.
- **Expected:** one "contains a link" answer. Today a task that toggles `block_links`
  on the task (policy/blue path) blocks `tg://` while the same toggle on an attached
  FilterRule (FLT path) does not, and the two carry different reason codes
  (`link_blocked` vs `links_blocked`) into the audit log
  (`pipeline.py:960-966` vs `pipeline.py:987-1003`).
- **Severity:** MEDIUM (silent under-blocking on one path, reason-code noise on both).
- **Recommended fix (separate PR):** make `FilterEngine.evaluate` use `has_link` for
  its `block_links` gate (and the `blocked_media_types:["link"]` sweep at
  `services.py:68-71`, which has the same narrower predicate), keeping the reason
  codes aligned.

### P-FLAGDUP — task-level `allowed_media_types` and the attached FilterRule's `allowed_media_types` are separate gates that can contradict (LOW)

- **Fixture:** task with `allowed_media_types=["photo"]` (`entities.py:407` column) and
  an attached filter with `allowed_media_types=["video"]`; a `video` message.
  - Python pipeline: `FilterEngine.evaluate` step 4 drops it at `pipeline.py:918`
    (`media_not_allowed:video`) before the task-level gate at `pipeline.py:956` ever
    runs. A `photo` message passes the filter and is then dropped by the task gate
    (`media_not_allowed:photo` from `media_filter.py:87`).
- **Expected:** an owner editing one list has no reason to believe the other list
  exists. The Filters screen badge (`filter_active_option_count`,
  `entities.py:1014-1031`) counts the six block flags plus "one when an advanced filter
  rule is attached", which is the only hint.
- **Severity:** LOW (configuration footgun, not a divergence between engines).
- **Recommended fix (separate PR, docs/UX):** surface the attached filter's media lists
  on the Filters screen, or unify the evaluation so the task-level allow-list is the
  single authority.

---

## 9. Fixtures proposed

Coordinator builds the JSON; these are one-line descriptions keyed by scenario id.

| id | description |
|---|---|
| FLT-FX-01 | FilterRule with `block_links=true` and a matching `regex_block`; assert the deciding gate is `link_blocked` (Python order) — will fail on Go today (P-ORD). |
| FLT-FX-02 | FilterRule with an invalid non-ASCII regex pattern; assert the `invalid_regex_block:` reason is character-truncated identically in both engines (P-TRUNC). |
| FLT-FX-03 | FilterRule `min_message_length=5` on a photo message with an empty caption; assert `too_short` (documents that the gate reads `effective_text`, which is `""`). |
| FLT-FX-04 | FilterRule `regex_require` whose only match is past byte 8192 of a long text; assert Python drops and Go allows (P-BOUNDED). |
| FLT-FX-05 | `blocked_media_types=["link"]` on text containing only `tg://resolve?domain=x`; Python filter allows, task-level `link_block_reason` drops (P-LINKGATE). |
| FLT-FX-06 | FilterRule with `whitelist_keywords=[]` (empty list); assert ALLOW — an empty whitelist is no constraint, distinct from a non-empty list that matches nothing. |
| FLT-FX-07 | Service message with an attached filter whose `drop_service_messages=false`; assert ALLOW (already pinned as fixture 17 — keep). |
| FLT-FX-08 | Legacy `regex_patterns` on a filter with BOTH blacklist and whitelist keywords; assert require-any routing (`services.py:108-111`). |
| POL-FX-01 | Task policy `{enabled: true}` with zero rules; assert inactive/`policy_inactive` — pins the `enabled=True`⇒`has_rules` semantics. |
| POL-FX-02 | Policy with `blacklist_off=true` and a matching blacklist word AND a matching `regex_block` pattern; assert both are skipped (`policy_passed`). |
| POL-FX-03 | Policy with `whitelist_mode=all` and words `["foo","bar"]` on text containing only `foo`; assert `whitelist_missing:bar`. |
| POL-FX-04 | Policy with `whitelist_mode=phrase`, words `["foo","bar"]`, on `"bar foo"`; assert `whitelist_phrase_missing` (order sensitivity). |
| POL-FX-05 | Policy `remove_links=true` on `"click [this](https://a.example/x) now dm @bob"`; assert `"click this now dm @bob"` and `{"links_removed": true}` (P-LNK baseline). |
| POL-FX-06 | Target override `{strip_words:["cat"]}` on `"the concatenate sat"`; Python leaves the text, Go deletes — P-STRP baseline. |
| POL-FX-07 | Target override with blacklist words (not just RemoveLinks/StripWords); assert the ENQUEUE stage re-decides it while the Go worker's projection does not (documents the `transform.go:42-49` projection by design). |
| POL-FX-08 | Arabic-kaf spelling of a Persian blacklist keyword; assert the variant class makes it match (`_VARIANT_CLASS` both engines). Already partly covered by fixture 14 for the filter path — extend to the policy path. |
| MED-FX-01 | `block_voice=true` on an `audio` message; assert `voice_blocked` (both flags cover both types — pins the pair coverage). |
| MED-FX-02 | `allowed_media_types=["photo"]` AND `block_video=true` on a video message; assert the allow-list reason wins (precedence). |
| MED-FX-03 | `block_text=true` on a media message with an empty caption whose `media_type` reports `text`; assert whether it drops (boundary between the media gate and the caption). |
| SET-FX-01 | A settings POST with `content_policy_enabled=false` on a task whose policy was active by `has_rules`; assert the policy goes off (pins the registry/entity default mismatch, POL-001). |
| SET-FX-02 | A settings POST with `delivery_transport="BOT"` and no `bot_token_id`; assert `invalid_state` and that nothing was written (pair atomicity, `RS:553-566`). |
| SET-FX-03 | `regex_block` list with a nested-quantifier pattern `(a+)+`; assert the write is rejected with `invalid_choice` (`RS:376-383`). |
| SET-FX-04 | A full-settings POST (all 76 keys) where no value moved; assert `PIN_INDEX` is not written ("a client that posts a whole settings object does not pin the settings it merely rendered", `RS:584-585`). |
| RT-FX-01 | Two rules of different owners matching the same message, higher priority second; assert both owners' rules match (per-owner single-match, `services.py:643-653`). |
| RT-FX-02 | `message_category="CHANNEL_POST"`; assert it behaves like ALL (`services.py:539-548` — documents the unimplemented category). |
| RT-FX-03 | Fan-out to two targets where one is the source; assert the message still reaches the other target (`services.py:566-572` every-target loop rule). |
| RT-FX-04 | `detection_criteria.regex_pattern` invalid in ALL mode; assert `invalid_regex` rejection without an exception (`services.py:480-482` — no ReDoS guard on this path). |
| USR-FX-01 | Task with `blacklist_users=["123"]` and a message from sender 123; assert the message is **not** rejected (STORED_BUT_UNUSED, §4 item 4). |

---

## 10. Unknowns

| id | what | why not verified |
|---|---|---|
| UNK-001 | Whether `debug_mode` (`SET-030`), `process_raw_text` (`SET-032`), `button_to_text` (`SET-055`), `news_digest` (`SET-045`), `translate_lang` (`SET-037`) have readers outside `core/application`, `core/domain`, `core/infrastructure/telegram` | Grep covered the core tree and the bot UI; a reader may exist in a surface not searched (e.g. a plugin directory, the web frontend's server, or the Flutter app's API calls — none of which are `core/`). Status recorded as STORED_BUT_UNUSED **within the engine decision path**, not repository-wide. |
| UNK-002 | Whether `blacklist_users`/`whitelist_users` were ever wired in a legacy delivery path | Repo history was not searched beyond the current SHA; the keys exist in the settings registry (`RS:189-193`) and the bot UI counts them (`pro_bot_ui.py:3085`), so they are customer-visible configuration that does nothing today. A git-log search would settle whether a reader was deleted. |
| UNK-003 | Go reason-string behaviour for a `regex_block` reason that exceeds 48 bytes with a trailing multi-byte char | Read from source (`truncate` at `filter.go:233-238` slices bytes); not executed for the 48-byte case specifically (only the 32-byte case was reasoned about). Fixtures should cover both. |
| UNK-004 | Whether the pipeline FILTER stage's `media_block_reason`/`link_block_reason` and `FilterEngine.evaluate` can double-count one message as `filtered_total` | `pipeline.py:923` increments on a filter drop and `pipeline.py:961` on a media drop; they are on disjoint code paths (a filter drop returns first at `:928`), so no double count — but `pipeline.py:1208` also increments for a target-scoped rejection, which is a third counter increment for one ingested message. Metrics-only; not a behavioral decision. |
| UNK-005 | `rt` — whether the Go worker's branding hop re-checks `matches_source` | Not in scope of the RT audit (the hop is C/D-owned) and not read here. |
| UNK-006 | The exact SQL `filter_rules` schema for the Postgres store | Only `sqlite_repositories.py:961-1010` was read (the SQLite DDL and row mapping). `postgres_repositories.py` presumably mirrors it; the FILTER audit does not depend on the schema, only on the entity (`entities.py:2312-2331`). |

---

## 11. Status roll-up

| prefix | features | status | basis |
|---|---|---|---|
| FLT | FLT-001..012 | `FULLY_IMPLEMENTED` both engines, with P-ORD (gate order), P-BOUNDED (ReDoS ceiling) and P-TRUNC (reason truncation) divergences | §2a, §7a |
| POL | POL-001..007 | `FULLY_IMPLEMENTED` Python; Go `PARTIAL` by design (projection, per `delivery.go:193-199`), with P-LNK / P-STRP content divergences on the target-override rewrite | §3, §7c |
| MED | MED: 20 types, 5 flags, precedence | `FULLY_IMPLEMENTED` Python; `ABSENT` in Go (ingest-side, by design) | §5 |
| RT | RT-001..006 | `FULLY_IMPLEMENTED` Python (except `CHANNEL_POST`/`USER_MESSAGE` categories → `PARTIALLY_IMPLEMENTED`, §6a); `ABSENT` in Go by design | §6 |
| SET | SET-001..076 (76 keys) | `FULLY_IMPLEMENTED` as a registry; 12 keys have no engine reader found (SET-030/032/033/034/037/040/041/045/055 plus UI-only rows) → `STORED_BUT_UNUSED` within the decision path; `go` parity `ABSENT` for all SET rows except `delivery_transport`/`bot_token_id` (`rule.go:62/68`) and the branding projection | §1 |
