# Agent B — Cleaner & Replace

## Mission
Stage 4.0 discovery-only audit of AutoTelegramForward (repo: /root/AutoTelegramForward, branch `audit/complete-business-logic-inventory`, baseline master `35ee530`). One of four parallel agents. **Audit only — do NOT rewrite engines, change schemas, or change runtime behavior.**

## Your scope (feature prefixes)
`CLN-*` (Cleaner), `RPL-*` (Replace engine), `UNI-*` (emoji/unicode), `LNK-*` (cleaner-side only: `remove_links` flag + `link_replacement`).

## Hard rules
- Cite `file:line` for every behavioral claim.
- Never edit any file outside `docs/behavior-audit/_handoff/b-cleaner-replace.md`.
- Never edit `docs/feature_catalog.json` or another agent's handoff.
- Never write Go/Python production code; never `git commit`/`git push`.
- `FULLY_IMPLEMENTED` requires a pasted production call chain reaching the code.

## Required deliverable
ONE file: `docs/behavior-audit/_handoff/b-cleaner-replace.md` with these sections:

### 1. Cleaner master switch + options (CLN-*)
Read `ForwardRule.build_cleaner()` at `core/domain/entities.py:808-880` and `CleanerRule` at entities.py:2335-2406. Document:
- Master switch `cleaner_enabled` (entities.py:927) — note it derives True from legacy flags.
- `cleaner_config` blob (entities.py:796) merging with the 8 legacy flag fields: `remove_links`, `clean_usernames`, `clean_hashtags`, `clean_phones`, `clean_buttons`, `clean_empty_lines`, `remove_emojis`, `clean_custom_words`.
- The regexes: `_CLEANER_URL_RE` (97), `_CLEANER_MENTION_RE` (101, `@[A-Za-z0-9_]{4,64}`), `_CLEANER_HASHTAG_RE` (102, `#\w+`), `_CLEANER_EMOJI_RE` (103), `_CLEANER_BLANK_LINES_RE` (114), `_strip_phone_numbers` (117-133, candidate+callback, 8-15 digits, separator or `+`).
- Execution order inside `CleanerRule.clean()` (entities.py:2380-2405): patterns → urls → mentions → hashtags → emojis → phones → whitespace collapse → empty lines → `\n{3,}` collapse → strip.
- `apply_to_caption` and `strip_buttons` (markup, applied in pipeline at `core/application/pipeline.py:1424`).
- Production call path: `pipeline.py:1408` in `_apply_transforms`. Failure policy: never drop (pipeline.py:1437).
- **Crucial**: the durable pipeline uses `CleanerRule.clean()`, NOT `FilterEngine.transform_text` — document the difference from the legacy `FilterEngine` cleaner helpers (`core/domain/services.py:178-237`), which are only reached via `sync_engine.py:261` (edit sync) and gRPC preview.

### 2. Emoji audit (UNI-EMOJI etc.)
There are **two** emoji regexes that must both be documented:
- `EMOJI_REGEX` at `core/domain/services.py:31-34`: `[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]|[\ufe00-\ufe0f]`
- `_CLEANER_EMOJI_RE` at `core/domain/entities.py:103-106` (identical)
- Go port: `worker/internal/engine/transform/transform.go:33` `emojiRegex`.
Analyze the Unicode coverage: which categories are removed (all SMP supplementary plane = ALL non-BMP chars including non-emoji like 𒀀 (Cuneiform), mathematical alphanumerics), variation selectors FE00-FE0F, misc symbols 2600-27BF, misc technical 2300-23FF (includes ⌂ ⌐ ⌘ keyboard symbols), 2B50-2B55 stars. What is NOT removed: skin-tone modifiers (1F3FB-1F3FF are IN the supplementary range so they ARE removed — but only as part of a sequence; document ZWJ sequence behavior), regional indicator symbols 1F1E6-1F1FF (flags — removed as supplementary), keycap base characters, combining marks. Run small Python checks to verify (write a scratch script under /tmp, never in the repo) e.g. does `"𒀀abc"` lose the cuneiform char? Does `"👨‍👩‍👧"` become empty or leave ZWJ (U+200D, which is in BMP and NOT matched)? Document findings factually.

### 3. Replace engine (RPL-*)
Read `core/domain/text_replace.py` (789 lines) fully:
- `ReplaceEntry` (109): fields source/to/mode/flags/whole_word/name/enabled/priority/rid.
- Storage compat: `to_value()` (plain legacy string vs dict), `to_rule_value()` (rid-keyed), `entry_from_value` (247), `normalize_map` (305), `parse_freeform` (530), `tokenize` (594), `parse_add_arguments` (646).
- Modes: literal vs regex (`MODE_*` constants at top of file); flags i/c/m/s/x; whole_word.
- `compile_entry` (350), `_legacy_literal` (365), `apply_entry` (374) — **exact-case-first then case-insensitive fallback** for legacy literals (385-390); invalid pattern → skip entry with warning (378); re.error → text unchanged (395).
- `apply_replacements` (415): stable sort by `-priority` (435), sequential application.
- `_literal_replacement` (402): literal entries escape backslashes; regex entries pass `\1` through.
- `stored_entries` (440), `legacy_rule_id` (459, sha1-based), `find_key`, `describe_entry`, `count_matches` (732), `merge_entry` (773).
- Production path: `FilterEngine.replace_text` → `apply_replacements` (`core/domain/services.py:229`), called from `core/application/pipeline.py:1543`. Management: `core/application/replace_rules.py`, `core/infrastructure/grpc_server/replace_servicer.py`, `core/infrastructure/telegram/replace_bot_ui.py`, `replace_fast_add.py`.
- Document the `#` → `##` then `##` → `x` priority/order example from the brief §8.3.

### 4. Cleaner-side link handling (LNK-CLEANER)
- `remove_links` flag as seen by cleaner (entities.py `remove_links` property at 402; `build_cleaner` merge at 1033).
- `link_replacement` (entities.py:1650) — used at `pipeline.py:1456` via `FilterEngine.replace_links` (services.py:222-230) — note this is a SEPARATE code path from `LinkManagementEngine` (Agent D owns LME).
- `FilterEngine.remove_urls` / `remove_links` helpers (services.py:162-200) and where each is called.

### 5. Go parity
`worker/internal/engine/transform/transform.go`: `RemoveEmojis` (137), `Hashtags` (127), `Usernames` (132), `Tidy` (71), `Clamp` (94), `ApplyTargetOverride` (56), `urlRegex` (21), `usernameRegex` (23), `hashtagRegex` (24), `phoneRegex` (25). **Verify reachability**: grep shows `RemoveEmojis`/`Hashtags`/`Usernames` have NO production callers (only `delivery.go:87,120` construct the Engine; `ApplyTargetOverride` is the only method used). Document that honestly with the grep evidence. Compare regexes to Python (e.g. Python `_CLEANER_MENTION_RE` is `{4,64}`, Go `usernameRegex` is `{3,32}`; Python hashtag `#\S+` in FilterEngine vs `#\S+` in cleaner `#\w+` vs Go `#[^\s#@]+`; phone regexes differ substantially).

### 6. Parity findings + fixtures + unknowns
Each finding: minimal input fixture, Python output, Go output, expected behavior, severity, recommended separate fix. Cover at least: the mention-regex `{4,64}` vs `{3,32}` divergence; hashtag `#\w+` vs `#[^\s#@]+`; phone regex divergence; emoji regex ZWJ/variation-selector behavior; `Tidy` vs `_tidy` differences.

## Starting points
`core/domain/entities.py` (97-133, 808-1110, 1650, 2335-2406), `core/domain/text_replace.py`, `core/domain/services.py` (162-237), `core/application/pipeline.py` (1401-1460), `worker/internal/engine/transform/transform.go`.
