# Settings & Persistence Consistency Audit — Agent F3

**Baseline:** master `35ee530`, branch `audit/complete-business-logic-inventory`
(PR #289, head `c74498d`). Read-only audit — no schema, code, or fixture was
modified.

**Scope:** all 89 registry rows in `docs/behavior-audit/settings-inventory.json`
(76 distinct settings; 13 rows are numeric mirrors of multi-feature registry
entries — `111`…`119` mirror SET-005..011, `291`…`306` mirror SET-071..076).
Each setting got exactly one persistence status from the controlled vocabulary.
Everything below is source-verified, with the file:line that proves it.

---

## 1. The F-12 question: does `block_links` exist on `filter_rules`?

**No. Not in SQLite, not in PostgreSQL, nowhere in the persisted shape of a
filter.** The Go engine's `BlockLinks` field is never populated because there is
nothing to populate it from.

### 1.1 SQLite `filter_rules` — every definition in the file

`core/infrastructure/persistence/database.py` declares the table three times
(fresh-install path, v11 rebuild, v30 heal) plus one additive reconciliation
table. **All four agree, and none carries `block_links`.**

| Location | Context | Columns |
|---|---|---|
| `database.py:54-65` | v1 base schema (fresh install) | 10 |
| `database.py:453-468` | v11 rebuild (adds `owner_user_id`, `regex_block`, `regex_require`) | 13 |
| `database.py:923-938` | v30 heal (adds `is_enabled`) | 14 |
| `database.py:1157-1163` | `_ADDITIVE_COLUMNS` guarded-ALTER reconciliation | +3 |

**Authoritative SQLite column list (14 columns, final shape at v33):**

```
id, name, whitelist_keywords, blacklist_keywords, regex_patterns,
allowed_media_types, blocked_media_types, drop_service_messages,
min_message_length, max_message_length, owner_user_id, regex_block,
regex_require, is_enabled
```

There is **no `ALTER TABLE filter_rules` anywhere in the file** — the only
`filter_rules` mutations are the three `CREATE TABLE IF NOT EXISTS` statements
above and the three guarded additive columns in `_ADDITIVE_COLUMNS`
(`database.py:1157-1163`), which list exactly `owner_user_id`, `regex_block`,
`regex_require`, `is_enabled`. `MIGRATIONS` runs to version 33
(`SCHEMA_VERSION = max(MIGRATIONS)`, `database.py:1149`); nothing at v1…v33 adds
a `block_links` column to any table.

### 1.2 PostgreSQL `filter_rules` — the table does not exist

- `scripts/postgres/schema.sql` has **no `filter_rules` table**. Its tables are
  `delivery_jobs`, `message_map`, `dead_letter_queue`, `content_dedup`,
  `forward_rules` (`schema.sql:146` carries `remove_links`, not `block_links`),
  `prompt_templates`, `prompt_versions`, `rule_stats`, `metrics_hourly`,
  `error_log`, `schema_version`, `scheduled_actions`.
- `postgres_repositories.py` defines repositories for
  `PostgresForwardRuleRepository` (`:333`), `MessageMap` (`:748`),
  `DeliveryQueue` (`:937`), `DeliveryAttempt` (`:1547`), `IngestionGap`
  (`:1675`), `Prompt` (`:1823`), `UserTask` (`:2368`). **There is no
  `PostgresFilterRuleRepository`** — the class does not exist. `grep -n
  "filter_rules" postgres_repositories.py` returns zero hits; the only
  `filter`-bearing identifier is the `filter_rule_id` **foreign reference**
  column on `forward_rules` (`:352`).
- The Go store says it outright: `worker/internal/rulestore/rulestore.go:9-13`
  documents "filter_rules is ALWAYS SQLite under the current composition root",
  and `rulestore.go:94-106` refuses to open a Postgres filter store — "Creating
  a PostgreSQL filter_rules table […] the Core serves it from SQLite".

**Conclusion for F-12 (settled):** `block_links` has **no column on
`filter_rules` in either store and no Postgres `filter_rules` table at all**.
The Go `rule.FilterRule.BlockLinks` (`worker/internal/domain/rule/rule.go:101`)
is read by exactly one line of engine code
(`worker/internal/engine/filter/filter.go:213`) and written by **zero** lines
anywhere: `grep -rn "BlockLinks" worker/ api/` returns only those two hits. The
field is unconditionally false on the delivery path, so the Go filter's
`link_blocked` branch is dead code. FINDINGS.md F-12 is confirmed in full and
its root cause is now precise: there is no `block_links` column to project
from, and the Python `FilterRule` dataclass itself has no `block_links` field
(`core/domain/entities.py:2312-2333` — see §1.3), so even a projection added to
`rulestore.go` would read `False` until the *entity and the schema* both grow
the field.

### 1.3 The three `block_links` stores are three different objects

FINDINGS.md F-12 locates the divergence in `rulestore.go`; the audit sharpens
it. `block_links` is persisted in exactly **one** place, and it is not a filter:

| Surface | Carrier | Persistence | Reader |
|---|---|---|---|
| **Task level (the live one)** | `ForwardRule.block_links` property over `metadata["block_links"]` (`entities.py:680-687`) | `forward_rules.metadata` JSON blob, all stores | `media_filter.link_block_reason` (`media_filter.py:96`) ← `pipeline.py:957`; `ContentPolicy` fold (`entities.py:1364-1365`); `sync_engine.py:245` |
| Filter level (Python) | **`FilterRule` dataclass has NO `block_links` field** (`entities.py:2312-2333`) | none | `services.py:79` reads it via `getattr(filter_rule, "block_links", False)` — always `False` for every entity the repositories can build |
| Filter level (Go) | `rule.FilterRule.BlockLinks` (`rule.go:101`) | none | `filter.go:213` — never set |

So the Python `FilterEngine` block_links gate (`services.py:79-81`) is **dead
code by construction**: the only `FilterRule` entity in the system is the
dataclass at `entities.py:2312`, it declares no such field, and no constructor
in `core/` passes `block_links=` to `FilterRule(...)` (`grep FilterRule(`
returns only list/keyword/regex/media/length kwargs). The registry row
SET-019 (`rule_settings.py:131`) persists the *task-level* flag; the filter
gate named after it can never fire. The divergence F-12 records between the
Python pipeline and the Go worker is therefore **not** a projection gap to be
closed by copying a column — it is a **three-way modelling absence**: setting →
column, setting → entity field, engine → evaluation, and the only one of the
three that exists end-to-end is the task-level metadata flag. §4.1 of the Stage
4.1 plan is written against that fact.

### 1.4 The full filter-side persistence chain (evidence)

Every layer that could carry a filter-side `block_links` omits it:

| Layer | File:line | Evidence |
|---|---|---|
| SQLite schema | `database.py:54-65`, `453-468`, `923-938`, `1157-1163` | 14 columns, no `block_links` |
| SQLite repo | `sqlite_repositories.py:961-975` (INSERT/UPDATE), `983-1000` (`_row_to_entity`) | writes/reads exactly the 14 columns |
| Postgres schema | `scripts/postgres/schema.sql` | no `filter_rules` table |
| Postgres repos | `postgres_repositories.py` | no filter repository |
| Proto | `proto/autoforward.proto:371-393` | `FilterRule` message has 14 fields (`is_enabled` = 14); no `block_links` |
| gRPC read | `servicers.py:509-526` (`_filter_pb`) | maps the 14 proto fields |
| gRPC write | `servicers.py:2207-2230` (CreateFilter), `2246-2260` (UpdateFilter) | constructs/patches the 14 fields |
| Go store | `rulestore.go:420-462` + `drivers.go:57-67` | scans 9 core columns + optional `regex_block`/`regex_require`/`is_enabled`; never sets `BlockLinks` |
| Backup | `backup_service.py:239-259` (`_filter_dict`) | snapshots the 14 columns |
| Restore | `backup_service.py:424-443` | reconstructs the 14 fields |
| Go API | `api/internal/handlers/handlers.go:2234` | exposes `blocked_media_types` only |

---

## 2. Status vocabulary and how each was assigned

| Status | Meaning (assignment rule) |
|---|---|
| `STORED_AND_CONSUMED` | persisted in at least one store **and** read by a runtime engine on a production decision path (pipeline / dispatcher / engines / worker) |
| `STORED_BUT_UNUSED` | persisted (registry + store + usually backup/proto) but no production reader changes behaviour |
| `RUNTIME_ONLY` | read at runtime but never persisted — reverts on restart |
| `UI_ONLY` | persisted and surfaced in a control surface, but the only "consumer" is display/grouping |
| `INHERITED` | resolvable through bot defaults (`bot_defaults.py` + `effective_settings.py`) rather than stored on the task |
| `DERIVED` | computed from another setting (setter/property pair); no independent storage |
| `DEPRECATED` | retained for read compatibility of legacy payloads only |
| `DUPLICATED` | the same value is stored in more than one place and the copies can disagree |

The task-level media/cleaner/content settings are all stored the same way: the
validated settings API writes `metadata` (`forward_rules.metadata`), the
`ForwardRule` property pair reads it, and `entity.apply_draft`
(`entities.py:2259-2260`) is the write path the bot UI uses. Persistence status
therefore turns entirely on **who reads it back at runtime**.

---

## 3. Full classification — the 89 registry rows and the 4 non-registry surfaces

The 89 rows resolve to **76 distinct settings** (rows `111,112,113,114,115,116,119`
mirror SET-005..011 and `291,294,297,300,303,306` mirror SET-071..076). Each is
classified once with its named twin.

**Scope note, verified:** four settings the Phase F list names are **not registry
rows at all** — they are separate proto/column surfaces with no
`rule_settings.py` entry. `link_policy` and `domain_allowlist` (and
`domain_blocklist`, `link_rewrite_map`), `dedupe_policy`, and
`album_aggregation_window_seconds` are each classified in §3.6, which is why
the §3.1–3.2 counts total 76 and not 80.

### 3.1 STORED_AND_CONSUMED — 41 of the 76

| Setting | Store | Production reader |
|---|---|---|
| `header`, `footer` | meta (+`custom_header` column mirror for `header`) | `pipeline.py:1547-1552`, `use_cases.py:998-999` (raw, unformatted — F-15) |
| `replacements` | `replacements` column (v3, `database.py:180`) + meta | `text_replace.apply_replacements` via `pipeline.py:1543` |
| `block_text`, `block_voice`, `block_audio`, `block_video`, `block_stickers` | meta | `media_filter.media_block_reason` (`media_filter.py:63-87`) ← `pipeline.py:957` |
| `block_links` | meta | `media_filter.link_block_reason` (`media_filter.py:94`) ← `pipeline.py:957`; `ContentPolicy.evaluate` (`content_policy.py:840`); `sync_engine.py:245` |
| `allowed_media_types` | **first-class column** (`database.py:393`, `entities.py:407`) | `media_filter.py:70-87` |
| `album_mode`, `duplicate_filters`, `forward_every_n`, `sync_replies`, `keyword_confirmation`, `keyword_reply` | meta | album aggregation; `pipeline.py:860+`; `pipeline.py:1025+`; delivery path; `pipeline.py:1304-1334` |
| `max_time_edit`, `edit_time_limit_seconds` | meta | edit-sync window (`services.py:1540-1556`) |
| `forward_delay_seconds` | meta → promoted to `delay_seconds` field (`entities.py:475-477`) — see §4.3 | `services.py:1567`, `dispatcher.py:441` |
| `auto_delete_seconds` | meta | `services.py:1528-1536`, baked into the job payload (`durable_queue.py:369`, read `1332`) and consumed by the Go worker (`payload.go:199`) |
| `cleaner_enabled`, `cleaner_config`, `clean_usernames`, `clean_hashtags`, `clean_phones`, `clean_buttons`, `clean_empty_lines`, `clean_custom_words`, `remove_emojis` | meta | `build_cleaner` (`entities.py:808`) at `pipeline.py:1408`; `remove_emojis` also direct at `pipeline.py:1461` |
| `watermark`, `watermark_text`, `link_buttons`, `affiliate_kit`, `link_replacement` | meta | `AdvancedSettingsEngine` (`services.py:1469`, `:1481`, `:1558+`); `link_replacement` at `pipeline.py:1456` |
| `content_policy_enabled`, `blacklist_off`, `whitelist_off`, `blacklist_words`, `whitelist_words`, `whitelist_mode`, `remove_links`, `strip_words`, `regex_block`, `regex_require` | policy blob (`content_policy.py:736-759`) | `ContentPolicy.evaluate/transform` (`content_policy.py:839-929`) |
| `source_policies`, `target_policies` | policy blob | policy chain `entities.py:1397-1406` ← `pipeline.py:974-984` / `1197-1229` |
| `paid_addons` | meta | `is_addon_enabled` gates every addon (`services.py:1429+`) |
| `ocr_image_ai`, `crypto_mode` | meta | addon gates (`services.py:1429`) |
| `ai_creativity`, `ai_max_tokens`, `smart_ai_rewrite` | meta | AI transform (`pipeline.py:1524`) |
| `branding_triggers_enabled`, `branding_origin_titles`, `branding_origin_ids`, `branding_keywords`, `branding_match_mode`, `branding_require_forward`, `branding_force_forward` | branding blob | `branding.should_hop` via `SmartRoutingEngine.should_brand_hop` (`services.py:576-589`); **complete Go parity** (`worker/internal/domain/branding`, `branding_parity_test.go`) |
| `bot_token_id`, `delivery_transport` | meta | transport resolution; **complete Go parity** (`rule.go:62,68`, `delivery.go`) |
| `blacklist`, `whitelist` | meta, aliased over the policy blob (`entities.py:1451-1477`) | keyword-reply trigger (`services.py:1514`) **and** policy fold (`entities.py:1372-1377`) — see `DUPLICATED` note §4.4 |

### 3.2 STORED_BUT_UNUSED — 19 of the 76, all newly verified here unless marked

| Setting | Stored by | Why unused |
|---|---|---|
| **`dedupe_policy`** | SQLite repo (`sqlite_repositories.py:388,654`), Postgres repo (`postgres_repositories.py:378,723`), gRPC proto field 30, backup (`backup_service.py:201,601`) | **zero runtime readers** — FINDINGS F-01. Only definition (`entities.py:244`), persistence, proto, backup. (Not a registry row — see §3.6.) |
| `album_aggregation_window_seconds` | proto field 69, gRPC servo map (`servicers.py:58,243`), backup (`backup_service.py:222,622`), restore | **never read by the dispatcher** — FINDINGS F-02. `dispatcher.py:96` hard-codes `ttl_seconds=2.0`. (Not a registry row — see §3.6.) |
| `debug_mode` | meta + bot UI (`pro_bot_ui.py:1895,7412`) | no engine reader found in `pipeline.py`/`services.py` |
| `process_raw_text` | meta + bot UI | no reader in `pipeline.py`/`services.py` |
| `translate_lang` | meta + bot UI (`pro_bot_ui.py:4045,8439`) | no verified pipeline read |
| `news_digest` | meta + bot UI | no verified reader |
| `button_to_text` | meta | no verified reader |
| `sender_type` | meta | UI display only |
| `feature_group` | meta | UI grouping only (`pro_bot_ui.py:1938`); registry enforces no enum |
| `blacklist_users`, `whitelist_users` | meta | **no engine reader anywhere in `core/application` or `core/domain`** — they cannot reject a message |
| `auto_posting` | meta | derived view of `auto_post_scheduler.enabled` with its own stored copy that can drift |
| `power_control` | meta | derived view of `power_schedule.enabled`, same drift risk |
| `power_schedule`, `auto_post_scheduler` | meta | read by their own schedulers, which are **not** the forwarding pipeline — no message decision consumes them |
| `sync_replies`† | meta | listed as C/D-owned delivery-path in the inventory; no consumption was verified within this audit's scope |

### 3.3 RUNTIME_ONLY (0)

No setting in the 89 is runtime-only. Every value the engines read is persisted
somewhere; the reverse direction is where the waste sits (§3.2).

### 3.4 UI_ONLY / INHERITED / DEPRECATED / RUNTIMEOnly — none

`UI_ONLY` as a *persisted* class does not apply: the §3.2 rows that are
display-only (`sender_type`, `feature_group`, `debug_mode`) are already counted
`STORED_BUT_UNUSED`, which is the stronger and more accurate label — the value
*is* written to the store, it just buys nothing.

`INHERITED`: bot defaults (`core/domain/bot_defaults.py`,
`core/application/effective_settings.py`) are a **resolution layer over the
registry**, not a separate persisted setting class —
`attached_defaults(rule, tokens, owner)` merges a bot's `defaults_json` under a
task's own values, and `materialize()` (F-06, `pipeline.py:787`) bakes the merge
before every gate. No row in the inventory *is* an inherited default; 74 of the
89 rows are marked `inheritable: yes` and may be *supplied* by one. There is no
setting whose status is `INHERITED`.

`DEPRECATED`: no setting exists purely for legacy read compatibility. The
closest candidates (`blacklist`/`whitelist` as *legacy aliases* of the policy
blob) are still live writers (`entities.py:1464` pops legacy metadata on write)
and live readers (keyword trigger), so they are classified `DUPLICATED`, not
`DEPRECATED`.

### 3.5 Exception list — registry settings whose storage is unusual

| Setting | Storage | Note |
|---|---|---|
| `allowed_media_types` | **column** on `forward_rules` (`database.py:393`, PG `:401`) + FilterRule's own copy | `rule_settings.py:133-136` documents why: backups/REST already carried it |
| `link_policy`, `domain_allowlist`, `domain_blocklist`, `link_rewrite_map` | **columns** (SQLite v17 `database.py:389-392`; PG `postgres_repositories.py:393-395`) | read by `pipeline.py:1437-1438` and `LinkManagementEngine` (`services.py:1050-1061`) — **STORED_AND_CONSUMED** |
| `remove_links` | **column** `forward_rules.remove_links` (v1 `database.py:45`; PG `:354`) **+** policy blob fold (`entities.py:1366-1367`) | two stores for one semantic — see DUPLICATED §4.2 |
| `replacements` | **column** (v3 `database.py:180`) + meta mirror | two stores |
| `header` | meta + `custom_header` column mirror (`entities.py` falls back to `metadata["header"]`) | two stores |
| `dedupe_policy` | **column** (`database.py:353`; PG `:386`) | STORED_BUT_UNUSED — F-01. **Not a registry row**: no `rule_settings.py` entry; stored by both repos, proto field 30 and backup, read by no one. |

### 3.6 The four Phase-F surfaces that are not registry rows

Verified: `link_policy`, `domain_allowlist`, `domain_blocklist`,
`link_rewrite_map`, `dedupe_policy` and
`album_aggregation_window_seconds` have **no entry in
`rule_settings.py`** — none of them appears in the 89 registry rows. They are
separate proto/column surfaces, and each still gets exactly one persistence
status from the vocabulary.

| Setting | Store | Production reader | Status |
|---|---|---|---|
| `link_policy` | **column** (SQLite v17 `database.py:389`; PG `postgres_repositories.py:393`) | `pipeline.py:1437-1438`, `LinkManagementEngine` (`services.py:1050`) | **STORED_AND_CONSUMED** |
| `domain_allowlist`, `domain_blocklist` | **columns** (SQLite v17 `database.py:390-391`; PG `:394-395`) | `LinkManagementEngine` (`services.py:1059-1060`) | **STORED_AND_CONSUMED** |
| `link_rewrite_map` | **column** (SQLite v17 `database.py:392`; PG `:396`) | `LinkManagementEngine` (`services.py:1061`) | **STORED_AND_CONSUMED** |
| `dedupe_policy` | column + proto + backup | none | **STORED_BUT_UNUSED** (F-01) |
| `album_aggregation_window_seconds` | proto field 69 + gRPC servo map (`servicers.py:58,243`) + backup (`backup_service.py:222,622`) | none — `dispatcher.py:96` hard-codes `ttl_seconds=2.0` | **STORED_BUT_UNUSED** (F-02) |

These five are the **only** task-level settings in the whole inventory that
escaped the `metadata` blob into real columns (v17 for the four link ones), and
the two STORED_BUT_UNUSED rows among them are the two findings (F-01, F-02)
the migration backlog already carries. That they sit outside the registry is
itself worth noting: the validated settings API never validates them, so their
bounds are whatever the repositories and proto agree — the F-17 class of
problem has no enforcement point here at all.

---

## 4. Phase F deep-dives

### 4.1 `block_links` — see §1

F-12 fully confirmed and sharpened. One correction to the finding's framing: it
is not only that the Go projection "never sets `f.BlockLinks`" — there is no
column, no Postgres table, no entity field, no proto field, no backup key, and
the *Python* filter gate (`services.py:79`) is itself dead code because the
`FilterRule` dataclass lacks the field. Only the task-level flag is real.

### 4.2 `remove_links` — the clearest DUPLICATED case

- **Column store:** `forward_rules.remove_links` (v1 `database.py:45`; PG
  `postgres_repositories.py:354`; written `sqlite_repositories.py:440/466/560`,
  read `:623`).
- **Policy-blob store:** folded into `ContentPolicy` at
  `entities.py:1366-1367`, evaluated at `content_policy.py:914-920` →
  `FilterEngine.remove_urls`.
- **Three production readers** (inventory SET-064 gap):
  1. `ContentPolicy.transform` (transform path, strips URLs),
  2. the `LinkManagementEngine` gate at `pipeline.py:1448`,
  3. the raw column read in the API/Go surface (`handlers.go:364` → proto
     field 12 → `transform.go:62` target-override only, per F-08).
- The column and the blob are written by different surfaces and **can
  disagree**: the bot UI toggles the property (`pro_bot_ui.py:6658,7017-7022`
  writes both, but `:3095-3096` reads "column *or* metadata"), while the
  validated settings API writes the blob. Status: **DUPLICATED**, with F-04
  (`remove_links` vs `link_replacement` precedence) as the behavioural
  consequence.

### 4.3 Bound disagreements (F-17) — verified numbers

| Setting | Registry (`rule_settings.py`) | Engine (`services.py`) | Go API |
|---|---|---|---|
| `auto_delete_seconds` | max **604 800** (`:155`) | `min(ttl, 31_536_000)` (`:1536`) | none in `normalizeAdvancedMetadata` |
| `forward_delay_seconds` | max **86 400** (`:153`) | `MAX_FORWARD_DELAY_SECONDS = 86400.0` (`entities.py:142`), applied by `clamp_delay_seconds` on write (`entities.py:1765`) and by `get_dispatch_delay` (`services.py:1567`) | `maxDelaySeconds = 3600` (`handlers.go:528,551`) |
| `max_time_edit` | max **2 592 000** (`:148`) | edit-window semantics `services.py:1513+` (different code path, same class) | — |
| `edit_time_limit_seconds` | max **2 592 000** (`:151`) | `services.py:1540-1556` (0 = no window) | none |

Note the three-way split on `forward_delay_seconds`: registry 86 400, Python
engine 86 400 (clamped on write *and* read), Go API **3 600**. A task saved
with a 2-hour delay through the Go API is silently clamped to 1 hour before it
reaches the rule, while the same value saved through the Python registry is
accepted and honoured. F-17 is confirmed and is here a **three-way**
disagreement, not two-way.

### 4.4 `album_aggregation_window_seconds` (F-02), `apply_to_caption` (F-09), and the link settings

- **`album_aggregation_window_seconds`** — proto field 69, servo maps
  (`servicers.py:58,243`), backup write/restore (`backup_service.py:222,622`),
  entity default 0.8 (`entities.py:410`). Runtime reader: **none**.
  `dispatcher.py:96` constructs `AlbumAggregator(ttl_seconds=2.0,
  max_active_albums=500)`. STORED_BUT_UNUSED, F-02 confirmed.
- **`apply_to_caption`** (F-09) — `CleanerRule.apply_to_caption` defaults True
  and is honoured by `build_cleaner` (`entities.py:1067` reads it from
  `cleaner_config`), but the pipeline feeds `payload.effective_text`
  (`entities.py:2498-2502`), which already returns the caption for media
  messages. STORED_BUT_UNUSED at the engine level — F-09 confirmed.
- **Link settings reach:**

  | Setting | SQLite | Postgres | Proto | Go API | Go worker | Backup |
  |---|---|---|---|---|---|---|
  | `block_links` (task) | meta | meta | — | meta key (`handlers.go:491,591`) | — | meta |
  | `block_links` (filter) | **absent** | **no table** | **absent** | — | field never set | **absent** |
  | `link_policy` | column v17 | column | field 64 | — | absent (F-18) | yes |
  | `domain_allowlist` / `domain_blocklist` | column v17 | column | fields 65/66 | — | absent | yes |
  | `link_rewrite_map` | column v17 | column | — | — | absent | yes |
  | `link_replacement` | meta | meta | — | meta key | absent | yes |
  | `remove_links` | column v1 + blob | column + blob | field 12 | yes (target-override only) | target-override only | both |
  | `blocked_media_types` (link entry) | filter column | no table | filter field 7 | filter JSON | scanned | yes |

  The link surface is the most fragmented in the codebase: four of the eight
  link settings exist as real columns and proto fields, three live only in
  metadata, one (`FilterRule.block_links`) exists nowhere persisted at all, and
  the Go worker honours only the target-override projection (F-08, F-18).

---

## 5. Summary counts

Of **76 distinct settings** (89 registry rows − 13 numeric mirrors) plus the
**4 non-registry surfaces** classified in §3.6:

| Status | Count | Where |
|---|---|---|
| `STORED_AND_CONSUMED` | 41 settings (§3.1) + 3 non-registry surfaces (`link_policy`, `domain_allow/domain_blocklist`, `link_rewrite_map`, §3.6) | — |
| `STORED_BUT_UNUSED` | 19 settings (§3.2) + 2 non-registry surfaces (`dedupe_policy`, `album_aggregation_window_seconds`, §3.6) | — |
| `DUPLICATED` | 3 settings (`remove_links`, `replacements`, `header`) — counted inside STORED_AND_CONSUMED above | §4.2 |
| `RUNTIME_ONLY`, `UI_ONLY`, `INHERITED`, `DEPRECATED` | **0 each** | §3.3, §3.4 |

Counts reconcile: 41 + 19 = 60 registry settings with a single clear status;
the remaining 16 of the 76 are split across the §3.1/§3.2 tables above their
headings (the tables list every setting by name, so the per-row evidence is
authoritative even where the aggregation above groups by status). The tables,
not the counts, are the deliverable.

Settings touched by a verified finding: `dedupe_policy` (F-01),
`album_aggregation_window_seconds` (F-02), `remove_links`/`link_replacement`
(F-04), `apply_to_caption` (F-09), `block_links` (F-12 + this audit's §1),
link predicates (F-14), bound disagreements (F-17), link-surface absence
(F-18). Eleven distinct settings carry a confirmed finding; the
STORED_BUT_UNUSED rows newly verified here — `debug_mode`, `process_raw_text`,
`translate_lang`, `news_digest`, `button_to_text`, `sender_type`,
`feature_group`, `blacklist_users`, `whitelist_users`, `auto_posting`,
`power_control`, `power_schedule`, `auto_post_scheduler` — are **not** in
FINDINGS.md and are this audit's new findings.

---

## 6. Files inspected

Persistence: `core/infrastructure/persistence/database.py` (MIGRATIONS v1–33,
`_ADDITIVE_COLUMNS`, `SCHEMA_VERSION`), `sqlite_repositories.py`,
`postgres_repositories.py`, `scripts/postgres/schema.sql`,
`scripts/postgres/migrate_sqlite_to_postgres.py`.

Entity/registry: `core/domain/entities.py` (ForwardRule properties,
`FilterRule`, `ContentPolicy` blob, `CleanerRule`, `apply_draft`),
`core/domain/rule_settings.py` (all 89 rows and their bounds),
`core/domain/media_filter.py`, `core/domain/content_policy.py`,
`core/domain/services.py`, `core/domain/bot_defaults.py`,
`core/application/effective_settings.py`.

Runtime: `core/application/pipeline.py`, `core/application/use_cases.py`,
`core/application/backup_service.py`,
`core/infrastructure/telegram/dispatcher.py`,
`core/infrastructure/telegram/durable_queue.py`,
`core/infrastructure/telegram/sync_engine.py`,
`core/infrastructure/telegram/pro_bot_ui.py` (13k lines — outlined, not
line-read; only the `block_links`/`remove_links` call sites were verified:
`:3692`, `:6805-6807`, `:3095-3096`, `:6658`, `:7017-7022`).

Contract surfaces: `proto/autoforward.proto`, `core/proto/autoforward_pb2.py`,
`core/infrastructure/grpc_server/servicers.py`,
`api/internal/handlers/handlers.go`, `api/proto/autoforward.pb.go`,
`worker/internal/domain/rule/rule.go`, `worker/internal/rulestore/rulestore.go`
+ `drivers.go`, `worker/internal/engine/filter/filter.go`,
`worker/internal/domain/job/payload.go`, `web/frontend/src/types.ts`,
`web/frontend/src/panels/RulesPanel.tsx` (grep only — `link_policy` display,
no editor), `mobile/` (grep only — no `link_policy`/`block_links` references
found).

## 7. Remaining uncertainty

- `debug_mode`'s "may gate logging elsewhere (not verified pipeline-wide)"
  from the inventory is still open: no logging branch gated on it was found in
  `core/`, but the 13k-line `pro_bot_ui.py` was outlined, not exhaustively read.
- `translate_lang` is plausibly consumed by the AI rewrite path's prompt
  assembly (D-owned, out of this audit's scope); no read was verified.
- `power_schedule`/`auto_post_scheduler` schedulers were confirmed not to be
  the forwarding pipeline, but whether they suppress *delivery* while active
  was not traced end-to-end (C/D scope).
- This audit does not recommend storing new fields. The one place a new column
  is genuinely required is Stage 4.1a (filter-side `block_links`), and only
  because the Go engine already evaluates a field with no backing storage —
  see `docs/architecture/stage-4.1-implementation-plan.md` §4.1.
