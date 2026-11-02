# Agent B — Cleaner & Replace (CLN / RPL / UNI / LNK-CLEANER)

Stage 4.0 discovery-only audit. Branch `audit/complete-business-logic-inventory`,
baseline master `35ee530`. All evidence below is read from source with
`file:line` citations; every behavioral claim was additionally exercised in a
throwaway CPython session (scripts under `/tmp`, never in the repo) and the
observed output is pasted where it is a finding.

Scaling note on line numbers: `core/domain/entities.py` is cited at the lines
read on this branch. The file compiles and imports cleanly at the cited lines.

---

## 1. Inventory table — Cleaner (CLN-*)

| ID | Feature | Store | Read site | Write site | Production call path | Status |
|---|---|---|---|---|---|---|
| CLN-MASTER | Cleaner master switch `cleaner_enabled` | `metadata["cleaner_enabled"]` | `core/domain/entities.py:936-944` (getter, derives True from the 8 legacy flags) | `core/domain/entities.py:946-950` (setter) | `build_cleaner()` gate `entities.py:1019` → `pipeline.py:1410` → `CleanerRule.clean()` `entities.py:2371` | FULLY_IMPLEMENTED |
| CLN-CONFIG | `cleaner_config` blob (patterns, use_regex, remove_urls/remove_mentions/remove_hashtags/remove_emojis/remove_phone_numbers/remove_empty_lines/strip_buttons, apply_to_caption, case_sensitive, name) | `metadata["cleaner_config"]` | `entities.py:1005-1009` | `entities.py:1011-1015` | merged in `build_cleaner()` `entities.py:1017-1073` | FULLY_IMPLEMENTED |
| CLN-LEGACY-8 | The 8 legacy flag fields `remove_links`, `clean_usernames`, `clean_hashtags`, `clean_phones`, `clean_buttons`, `clean_empty_lines`, `remove_emojis`, `clean_custom_words` | `metadata[...]` (7 props) + first-class field `remove_links` (`entities.py:402`) | `entities.py:690-772` | `entities.py:693-772` (setters) | folded in `build_cleaner()` `entities.py:1024-1045`; registry `core/domain/rule_settings.py:111-119` | FULLY_IMPLEMENTED |
| CLN-PATTERNS | Custom word/pattern removal (literal or regex, case option) | `cleaner_config["patterns"]` + `clean_custom_words` | `entities.py:1032-1037` | bot panel / web draft | `CleanerRule.clean()` loop `entities.py:2376-2389` | FULLY_IMPLEMENTED |
| CLN-URL | URL stripping (`remove_urls` / legacy `remove_links`) | `cleaner_config["remove_urls"]` | `entities.py:1039` | — | `_CLEANER_URL_RE` `entities.py:97-100`, applied at `entities.py:2390-2391` | FULLY_IMPLEMENTED |
| CLN-MENTION | @mention stripping | `cleaner_config["remove_mentions"]` | `entities.py:1040` | — | `_CLEANER_MENTION_RE` `entities.py:101`, applied at `entities.py:2392-2393` | FULLY_IMPLEMENTED |
| CLN-HASHTAG | #hashtag stripping | `cleaner_config["remove_hashtags"]` | `entities.py:1041` | — | `_CLEANER_HASHTAG_RE` `entities.py:102`, applied at `entities.py:2394-2395` | FULLY_IMPLEMENTED |
| CLN-EMOJI | Emoji stripping (cleaner option) | `cleaner_config["remove_emojis"]` | `entities.py:1042` | — | `_CLEANER_EMOJI_RE` `entities.py:103-106`, applied at `entities.py:2396-2397` | FULLY_IMPLEMENTED |
| CLN-PHONE | Phone-number stripping | `cleaner_config["remove_phone_numbers"]` | `entities.py:1043` | — | `_strip_phone_numbers` `entities.py:111-131`, applied at `entities.py:2398-2399` | FULLY_IMPLEMENTED |
| CLN-EMPTY | Blank-line collapse | `cleaner_config["remove_empty_lines"]` | `entities.py:1044` | — | `_CLEANER_BLANK_LINES_RE` `entities.py:114`, applied at `entities.py:2403-2404` | FULLY_IMPLEMENTED |
| CLN-BUTTONS | Inline-keyboard removal (`strip_buttons` / legacy `clean_buttons`) | `cleaner_config["strip_buttons"]` | `entities.py:1045` | — | **markup, applied in the pipeline**, not in `clean()`: `pipeline.py:1422-1427` and `use_cases.py:958-961` (`payload.reply_markup = None`) | FULLY_IMPLEMENTED |
| CLN-CAPTION | `apply_to_caption` — clean media captions, not just bodies | `cleaner_config["apply_to_caption"]` (default **True**, `entities.py:1067`) | `entities.py:1067`, docstring `2352`, field `2366` | web UI `web/frontend/src/components/TaskOverviewModal.tsx:1394` | **STORED_BUT_UNUSED by the engine** — `pipeline.py:1410-1419` calls `cleaner.clean(payload.effective_text)`, and `effective_text` already returns the caption for media (`entities.py:2498-2502`). No code branch reads `apply_to_caption` to decide whether to clean the caption; the flag cannot turn caption cleaning off in the durable path. (Only a test constructs it: `core/tests/test_cleaner_and_profile_parity.py:200`.) | PARTIALLY_IMPLEMENTED |
| CLN-COUNT | Badge count of active options | derived | `entities.py:963-976` (`CLEANER_OPTION_FLAGS` at `953-961`) | — | UI badge; reads the same flags the engine reads | FULLY_IMPLEMENTED |
| CLN-DOUBLE-EMOJI | Second, redundant flag-path emoji removal in the durable pipeline | `rule.remove_emojis` read through `getattr` | `pipeline.py:1460` | bot panel `core/infrastructure/telegram/pro_bot_ui.py:6706`, web `RuleModal.tsx:66` | `FilterEngine.remove_emojis` `services.py:234-237` (EMOJI_REGEX `services.py:31-34`) | DUPLICATED (see UNI-DUP) |
| CLN-USECASE | Cleaner on the reply / private-view path | — | — | — | `use_cases.py:948-967` (same `build_cleaner()` + `clean()`, same never-drop policy) | FULLY_IMPLEMENTED |
| CLN-NODROP | Failure policy — a cleaner failure never drops the message | — | — | — | `pipeline.py:1428-1434`, `use_cases.py:962-967` | FULLY_IMPLEMENTED |
| CLN-EMPTY-DROP | Empty-after-clean drop with reason code | — | — | — | `pipeline.py:1468-1486` (`empty_after_clean`, audit `forward.filtered` / `EMPTY_AFTER_CLEAN`) | FULLY_IMPLEMENTED |

### 1a. Master switch and legacy derivation (CLN-MASTER, CLN-LEGACY-8)

Getter `entities.py:936-944`:

```python
if isinstance(self.metadata, dict) and "cleaner_enabled" in self.metadata:
    return bool(self.metadata["cleaner_enabled"])
if bool(self.remove_links or self.clean_usernames or self.clean_hashtags or self.remove_emojis
        or self.clean_custom_words or self.clean_phones or self.clean_buttons
        or self.clean_empty_lines):
    return True
return False
```

So an explicit `cleaner_enabled: False` in metadata **wins over** any legacy
flag: a legacy row that only ever set `remove_links` and is then saved through
a client that writes `cleaner_enabled: false` is silently disabled. Verified by
execution: `ForwardRule` with `metadata={"remove_links": True}` (metadata only,
first-class field left at default `False`).

**Important asymmetry found while verifying**: `remove_links` is a **first-class
dataclass field** (`entities.py:402`), not a metadata property like the other
seven. Its value is therefore only restored from the DB column
(`sqlite_repositories.py:623` `remove_links=bool(row["remove_links"])`;
`postgres_repositories.py:693`), **not** from `metadata["remove_links"]`. A raw
row that carries `remove_links` only inside the metadata blob has
`rule.remove_links == False`, so the `cleaner_enabled` legacy derivation at
`entities.py:940` returns False and `build_cleaner()` returns `None` — no
cleaning. The other seven legacy flags *are* metadata properties, so a raw
metadata row with `clean_usernames: True` does derive `cleaner_enabled = True`.
**Observed in the interpreter**: `metadata={"cleaner_enabled": True,
"cleaner_config": {"remove_mentions": True}}` → `cleaner_enabled` False,
`build_cleaner()` None; identical metadata plus `remove_links: True` in
metadata → still None (field wins). The pipeline does read the metadata copy in
one place (`pipeline.py:1438` `getattr(rule, "remove_links", False)` — the
field again, plus `services.py:270` checking `rule.metadata.get("remove_links")`
for the legacy `transform_text` path). The DB write path that keeps the two in
sync is `apply_draft` at `entities.py:2220-2222` (sets the field *and* writes
the metadata copy), so rows saved through the draft are consistent; only
hand-written/external metadata-only writes hit the dead flag.

### 1b. `build_cleaner()` merge semantics (CLN-CONFIG)

`entities.py:1017-1073`, verified by execution:

- Gate: `cleaner_enabled` false → `None` (`1019`).
- `cleaner_config` empty/absent and no legacy flag → `None` (`1022-1027`);
  empty config **with** a legacy flag proceeds with `raw_cfg = {}` (`1028`).
- Config key **wins over** the legacy flag: `bool(cfg.get("remove_urls",
  self.remove_links))` at `1039-1045` — verified:
  `cleaner_config={"remove_urls": False}` + legacy `remove_links=True` (field
  set) → `remove_urls=False`. (With the metadata-only `remove_links` the
  cleaner does not build at all — see §1a.)
- `clean_custom_words` are appended to `patterns` deduped, config patterns
  first (`1032-1037`): `{"patterns": ["a"]}` + `clean_custom_words=["b","a"]`
  → `patterns == ["a", "b"]`.
- `patterns` entries that are empty after `str().strip()` are dropped
  (`1047`); if no pattern and no option remains → `None` (`1048-1052`).
- `use_regex` / `case_sensitive` default **False** (`1059`, `1068`);
  `apply_to_caption` defaults **True** (`1067`).
- Any construction exception is swallowed → `None` (`1072-1073`).

### 1c. Execution order inside `CleanerRule.clean()` (verified)

`entities.py:2371-2406`. The handoff §1 order is confirmed, with two
corrections: the whitespace collapse and `\n{3,}` collapse are **always**
applied (not optional and not part of `remove_empty_lines`):

1. patterns, each `re.sub`/`str.replace` in list order, `re.error` → skip that
   pattern (`2376-2389`)
2. `remove_urls` → `_CLEANER_URL_RE.sub` (`2390-2391`)
3. `remove_mentions` → `_CLEANER_MENTION_RE.sub` (`2392-2393`)
4. `remove_hashtags` → `_CLEANER_HASHTAG_RE.sub` (`2394-2395`)
5. `remove_emojis` → `_CLEANER_EMOJI_RE.sub` (`2396-2397`)
6. `remove_phone_numbers` → `_strip_phone_numbers` (`2398-2399`)
7. **unconditional** `[ \t]{2,}` → `" "` (`2402`)
8. `remove_empty_lines` → `\n[ \t]*\n(?:[ \t]*\n)*` → `"\n"` (`2403-2404`)
9. **unconditional** `\n{3,}` → `\n\n` (`2405`)
10. `.strip()` (`2406`)

Observed end-to-end with every option on:
`"SECRET check https://t.me/x @user1 #tag 😀 +1 555 123 4567\n\n\n\nend"` →
`'check \nend'` (note the surviving single space where the URL was — the
`[ \t]{2,}` collapse cannot remove a single space).

`case_sensitive=False` (default) makes patterns case-insensitive:
`patterns=["secret"]` on `"SECRET word"` → `'word'`; with `case_sensitive=True`
→ `'SECRET word'`. Invalid regex with `use_regex=True` → pattern skipped,
text unchanged: `patterns=["(unclosed"]` → `'text'`.

### 1d. The regexes (verbatim)

| Name | Site | Pattern |
|---|---|---|
| `_CLEANER_URL_RE` | `entities.py:97-100` | `https?://[^\s]+|www\.[^\s]+|(?:https?://)?(?:t(?:elegram)?\.(?:me|dog))/[^\s]+` · `re.IGNORECASE` |
| `_CLEANER_MENTION_RE` | `entities.py:101` | `@[A-Za-z0-9_]{4,64}` |
| `_CLEANER_HASHTAG_RE` | `entities.py:102` | `#\w+` |
| `_CLEANER_EMOJI_RE` | `entities.py:103-106` | `[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]|[\ufe00-\ufe0f]` |
| `_CLEANER_PHONE_CANDIDATE_RE` | `entities.py:111` | `(?<![\d])\+?\d[\d\s\-().]{5,18}\d(?![\d])` |
| `_CLEANER_PHONE_SEPARATOR_RE` | `entities.py:112` | `[\s\-().]` |
| `_CLEANER_DIGITS_RE` | `entities.py:113` | `\D` |
| `_CLEANER_BLANK_LINES_RE` | `entities.py:114` | `\n[ \t]*\n(?:[ \t]*\n)*` |

`_strip_phone_numbers` `entities.py:117-131`: candidate match → strip
non-digits → accept only `8 <= len(digits) <= 15` (`123`); then remove when the
candidate starts with `+` after strip (`125-126`) **or** when it has ≥10 digits
and contains a separator (`127-128`). A bare 10-digit id is deliberately left
alone unless it carries a separator — comment at `entities.py:108-110`.

### 1e. Durable pipeline vs the legacy FilterEngine cleaner helpers

The durable path uses `CleanerRule.clean()`, **not** `FilterEngine.transform_text`:

- `pipeline.py:1408-1419` — `rule.build_cleaner()` → `cleaner.clean(text)`; on
  exception the text is delivered uncleaned (`1428-1434`).
- `use_cases.py:948-957` — the reply/private-view path, same cleaner and same
  order (comment at `944-947`: the cleaner must run on *every* forwarding
  path).
- The legacy helper set — `FilterEngine.remove_urls` (`services.py:159-169`),
  `remove_links` (`171-181`), `remove_usernames` (`183-189`),
  `remove_hashtags` (`191-197`), `remove_phones` (`199-205`),
  `remove_custom_words` (`207-217`), `clean_empty_lines` (`219-223`),
  `remove_emojis` (`234-237`) and the orchestrator `transform_text`
  (`services.py:263-314`) — has these production callers only:
  - `core/infrastructure/telegram/sync_engine.py:261` (edit-sync re-transform,
    `final_text = self._filter_engine.transform_text(...)`),
  - `core/infrastructure/grpc_server/servicers.py:1523` (dry-run/preview via
    `TransformationService.transform_text`, which delegates at
    `services.py:412`),
  - the content policy's `remove_links` transform calls
    `FilterEngine().remove_urls` (`content_policy.py:917`) — that is Agent A/D
    territory, noted only because it reuses a *helper*, not the orchestrator.
- Consequences of the split: the legacy orchestrator applies **custom words
  before** replacements and empty lines near last (`services.py:290-300`), and
  applies `link_replacement` before either; the durable path applies the
  cleaner first and replacements once, last (`pipeline.py:1543`), so an
  edit-sync and a fresh forward of the same rule can produce different text
  (see parity section CLN-P1).

### 1f. UI entries (context, not primary)

Web: cleaner tab in `web/frontend/src/components/TaskOverviewModal.tsx:1359-1418`
(master toggle at `1371`, option pills write straight into
`draft.cleaner_config` at `1400-1405` and force `cleaner_enabled: true` at
`1402`), types at `web/frontend/src/types.ts:99-100, 207-208`. Bot panel:
`core/infrastructure/telegram/pro_bot_ui.py:6706` toggles `remove_emojis`,
`9318` toggles the draft flag. Draft round-trip: `to_draft()` at
`entities.py:2053-2061`, `apply_draft` at `entities.py:2124-2139`.

---

## 2. Emoji / Unicode audit (UNI-*)

There are **two** Python copies of the same five-alternative pattern plus one
Go copy:

| ID | Site | Pattern |
|---|---|---|
| UNI-EMOJI-FE | `core/domain/services.py:31-34` (`EMOJI_REGEX`) | `[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]|[\ufe00-\ufe0f]` |
| UNI-EMOJI-CLN | `core/domain/entities.py:103-106` (`_CLEANER_EMOJI_RE`) | identical (comment at `95-96` says it is duplicated to keep entities free of a services→entities cycle) |
| UNI-EMOJI-GO | `worker/internal/engine/transform/transform.go:33` (`emojiRegex`) | `[\x{10000}-\x{10ffff}]|[\x{2600}-\x{27bf}]|[\x{2300}-\x{23ff}]|[\x{2b50}-\x{2b55}]|[\x{fe00}-\x{fe0f}]` — byte-identical semantics |

All three produce the same output on every fixture below (Python `_CLEANER_EMOJI_RE`
vs the Go ranges compared directly in the interpreter).

### 2a. What the pattern removes

| Input | Output | Reason |
|---|---|---|
| `𒀀abc` (Cuneiform, SMP) | `abc` | **non-emoji SMP content is collateral damage**: `U+10000-U+10FFFF` is the whole supplementary plane, not an emoji block |
| `𝔸𝕩 math` (mathematical alphanumerics) | ` math` | same — SMP |
| `hi 😀 there` | `hi  there` | intended |
| `👍🏽` (thumb + skin tone) | `''` (empty) | both codepoints are ≥ U+10000, so base + modifier both vanish |
| `🇺🇸` (regional indicators U+1F1FA U+1F1F8) | `''` | flags are supplementary → removed |
| `👨‍👩‍👧` (ZWJ family) | `'\u200d\u200d'` | the three emoji codepoints are removed, the **two ZWJs (U+200D) survive** — ZWJ is BMP and not matched. Output is two invisible joiners, not empty. |
| `5️⃣` (keycap: `5` + U+FE0F + U+20E3) | `'5⃣'` | the variation selector FE0F is stripped, the **keycap base `5` and combining enclosing keycap U+20E3 survive** → a dangling `5⃣` |
| `❤️` (U+2764 U+FE0F) | `' '` (heart gone) | U+2764 is in the 2600-27BF misc-symbols range, so the base glyph is removed by the *heart* alternative, not just the VS |
| `⌘ ⌂ ⌐` (misc technical 2300-23FF) | `'  '` | **keyboard/UI symbols are removed** — the 2300-23FF block is misc technical, not emoji |
| `⭐` (U+2B50) | removed | 2B50-2B55 stars |
| `← → ↑` (arrows, 2190-21FF) | unchanged | 2190-21FF is NOT in any alternative — arrows survive |
| `é` combining (e + U+0301) | unchanged | combining marks are BMP and outside every range |
| `a\u200db` (bare ZWJ) | unchanged | ZWJ not matched |

**Documented behavior**: the regex is a blunt "anything above the BMP plus four
BMP pictograph blocks" filter, not an emoji database. It removes Cuneiform,
Deseret, mathematical alphanumerics, Yi, tag characters, private-use
supplementary planes and musical symbols along with real emoji; it leaves
arrows (2190-21FF), combining marks, and — problematically — ZWJ and the
enclosing keycap, which produces broken sequences (UNI-ZWJ, UNI-KEYCAP).

### 2b. Where each copy is actually used

| ID | Path | Status |
|---|---|---|
| UNI-DUP | Cleaner option `remove_emojis` → `_CLEANER_EMOJI_RE` at `entities.py:2396-2397`, **and** — in the *same* durable pipeline, four lines later — the task flag `remove_emojis` → `FilterEngine.remove_emojis` at `pipeline.py:1460-1461` → `EMOJI_REGEX` at `services.py:234-237` | DUPLICATED. Both are reachable: the cleaner copy when `cleaner_config`/legacy flag builds a `CleanerRule`, the flag copy when `rule.remove_emojis` is true (bot panel writes it, `pro_bot_ui.py:6706`). Because the cleaner runs first at `pipeline.py:1408` and the flag path at `1460`, a task with both configured strips emoji **twice**. Harmless today (idempotent), but it means the emoji behavior is decided by two different config stores and two code sites. |
| UNI-LEGACY | `transform_text` step 4 `services.py:281-283` (edit-sync via `sync_engine.py:261`, gRPC preview via `servicers.py:1523`) | LEGACY_ONLY (still production on the edit path) |
| UNI-GO | `transform.RemoveEmojis` `transform.go:137-138` | **UNREFERENCED_HELPER** — see §5. |

Note: no Python or Go code performs Unicode property matching (`\p{Emoji}`,
grapheme clustering, or ZWJ-sequence handling) anywhere in the cleaner/emoji
path; the five alternatives are the only emoji logic.

---

## 3. Replace engine (RPL-*)

`core/domain/text_replace.py` (789 lines, read in full).

| ID | Feature | Site | Status |
|---|---|---|---|
| RPL-ENTRY | `ReplaceEntry` dataclass: `source`, `to`, `mode`, `flags`, `whole_word`, `name`, `enabled`, `priority`, `rid` | `text_replace.py:108-127` | FULLY_IMPLEMENTED |
| RPL-MODES | `MODE_LITERAL`/`MODE_REGEX` (`44-46`); flags `FLAG_CHARS = "icwmsx"` (`48-52`) | — | FULLY_IMPLEMENTED |
| RPL-BOUNDS | `MAX_ENTRIES=100`, `MAX_PATTERN_LEN=300`, `MAX_REPLACEMENT_LEN=2000`, `MAX_NAME_LEN=40`, `MIN/MAX_PRIORITY=0/1000`, `DEFAULT_PRIORITY=100` | `55-66` | FULLY_IMPLEMENTED |
| RPL-STORAGE | `to_value()` plain-string for legacy rows (`155-172`); `to_rule_value()` rid-keyed (`174-198`); `entry_from_value()` accepting legacy string, option dict, `{from,to}`, `{replacement}`, `ignore_case` bool, `whole_word`/`word`, `id`/`rid` (`247-281`) | FULLY_IMPLEMENTED |
| RPL-NORMALIZE | `normalize_map` (`305-331`, strict flag, keeps problem rows as-is non-strict) | FULLY_IMPLEMENTED |
| RPL-SMART-CASE | `_legacy_literal` (`365-371`) + `apply_entry` (`374-399`): exact-case `str.replace` first; only when the exact form is absent does the case-insensitive `re.sub` run | FULLY_IMPLEMENTED — observed: `{"Discount": "Sale"}` on `"discount 10% DISCOUNT 20%"` → `"Sale 10% Sale 20%"`, and (existing test) `{"Gold": "X"}` on `"Gold GOLD gold"` → `"X GOLD gold"` (`core/tests/test_text_replace_options.py:43`) |
| RPL-SAFE | invalid pattern → entry skipped with a warning (`387-393`); `re.error` at substitution time → text unchanged (`395-399`) | FULLY_IMPLEMENTED — observed: `{"re:(unclosed": "x"}` → `'text'` |
| RPL-LITERAL-ESC | `_literal_replacement` (`402-411`): literal entries escape backslashes so a stray `\1` renders literally; regex entries pass `\1` through as a backreference | FULLY_IMPLEMENTED — observed: `{"a": "\\1"}` → `'\\1 b \\1'`; `{"re:(\w+)\s+(\w+)": r"\2 \1"}` on `"hello world"` → `"world hello"` |
| RPL-ORDER | `apply_replacements` (`415-437`): stable sort by `-priority` (`433`), sequential application | FULLY_IMPLEMENTED |
| RPL-WHOLE-WORD | `_pattern_body` (`339-347`): `(?<!\w)` / `(?!\w)` lookarounds, omitted when the body is anchored `^`/`$` | FULLY_IMPLEMENTED |
| RPL-COMPILE | `compile_entry` (`350-362`) → guarded `compile_user_pattern` from `content_policy` (`text_replace.py:40`) | FULLY_IMPLEMENTED |
| RPL-MANAGER | `ReplaceRuleService` in `core/application/replace_rules.py` (create `453`, update `483`, delete `536`, toggle `554`, duplicate `558`, test `576`), owned reads/writes (`192`, `442-451`), rid-keyed copies (`419-440`) | FULLY_IMPLEMENTED |
| RPL-LEGACY-ID | `legacy_rule_id` sha1 of `task_id\0key` (`459-462`), `stored_entries` (`440-456`), `find_key` name-then-source (`470-487`), `describe_entry` (`490-495`), `count_matches` (`732-752`), `merge_entry` (`773-789`, name-collision replace, `MAX_ENTRIES` guard) | FULLY_IMPLEMENTED |
| RPL-PARSE-FREE | `parse_freeform` (`530-574`) with `DELIMITERS` `->`/``=>`/`➔`/`—>`/`→` (`69`) plus `:` for literal only (`71-72`), trailing `\| flags` via `_split_flags` (`503-511`), `re:`/`lit:` prefixes (`552-556`), `/…/` regex quoting (`563-564`) | FULLY_IMPLEMENTED |
| RPL-PARSE-CMD | `tokenize` (`594-643`, quote handling, `-"old"` dash-quote form, unterminated quote → `ReplaceError`) and `parse_add_arguments` (`646-729`, `--name/-n`, `-d/--delete`, `--flags=`, `--priority=/-p`, mode and flag aliases `581-592`) | FULLY_IMPLEMENTED |
| RPL-PIPELINE | Durable path: `pipeline.py:1542-1543` `self._filter_engine.replace_text(text, rule.replacements)` → `services.py:239-245` → `apply_replacements`. Single application, after AI and before header/footer (comment `1538-1541`) | FULLY_IMPLEMENTED |
| RPL-USECASE | Reply path: `use_cases.py:978-979` | FULLY_IMPLEMENTED |
| RPL-LEGACY-ORCH | Legacy orchestrator: `transform_text` step 7 `services.py:294-296` (edit-sync + gRPC preview) | LEGACY_ONLY |
| RPL-SURFACES | gRPC `core/infrastructure/grpc_server/replace_servicer.py`, bot panel `core/infrastructure/telegram/replace_bot_ui.py` (1157 lines), fast-add grammar `core/infrastructure/telegram/replace_fast_add.py` (`parse_replace_fast_add` at `92`, arrow-vs-legacy discrimination `is_arrow_form` at `76`) | FULLY_IMPLEMENTED (management surfaces; verified present, not exhaustively read — they are not engine behavior) |

### 3a. The priority/order example (brief §8.3), verified

`apply_replacements("# title", {"#": "##", "##": "x"})`
- insertion order `#` first, `##` second (both at default priority 100, stable
  sort keeps insertion order): `#` → `##` gives `## title`, then `##` → `x`
  re-matches the *produced* text → **`'x title'`**
- insertion order reversed (`##` first): `##` finds nothing, `#` → `##` →
  **`'## title'`**
- priorities override insertion order: `{"#": {"to": "##"}, "##": {"to": "x",
  "priority": 200}}` → `##` runs first → **`'## title'`**; priority on `#`
  instead → **`'x title'`**

So without an explicit priority the result depends on dict ordering the user
cannot see; the priority field is the documented escape hatch
(`text_replace.py:416-422`). This is the reason replacements are applied
exactly once at `pipeline.py:1543` (comment at `1488-1490`: "so an expanding
rule (`# -> ##`) is not applied twice").

### 3b. Interaction with the content policy's own replacement pass

For the record (Agent A owns `POL-*`): `ContentPolicy.transform`
(`content_policy.py:931-936`) runs a **second**, separate replacement pass via
`_apply_replacements` (`979-1006`) — a single-pass alternation, deliberately
*not* sequential ("`a=>b` followed by `b=>c` yields `b`, not `c`",
`content_policy.py:984-985`). The two replacement engines therefore have
**opposite composition semantics by design**: task `replacements` are
sequential/priority-ordered, policy `replacements` are single-pass.

---

## 4. Cleaner-side link handling (LNK-CLEANER)

| ID | Feature | Site | Status |
|---|---|---|---|
| LNK-CLEANER-FLAG | `remove_links` as the cleaner sees it: first-class field `entities.py:402`, read into the cleaner at `entities.py:1039` (`remove_urls = bool(cfg.get("remove_urls", self.remove_links))`) and into the cleaner master-switch derivation at `entities.py:940` | FULLY_IMPLEMENTED (with the field/metadata asymmetry of §1a) |
| LNK-CLEANER-URLRE | Cleaner's own URL pattern `entities.py:97-100` (ignores `www.` without scheme? no — it does match `www\.[^\s]+`; but it does NOT strip markdown `[text](url)`, unlike the legacy helper) | FULLY_IMPLEMENTED |
| LNK-CLEANER-REPL | `link_replacement` (`entities.py:1858-1866`, metadata string) → applied in the durable pipeline **only when the link engine is not engaged**: `pipeline.py:1455-1457` `elif getattr(rule, "link_replacement", None): text = self._filter_engine.replace_links(...)`, i.e. after `LinkManagementEngine.apply_to_payload` at `pipeline.py:1438-1454`. Legacy path: `transform_text` step 1 `services.py:268-269`. `replace_links` itself at `services.py:225-232` (markdown anchor → **replacement text**, then `URL_REGEX.sub(replacement)`) | FULLY_IMPLEMENTED — separate code path from LME (Agent D owns the engine; B owns the flag semantics only) |
| LNK-CLEANER-LEGACY | Legacy helpers: `FilterEngine.remove_urls` `services.py:159-169` (keeps markdown anchor text at `163`, strips raw URLs at `165`), `remove_links` `171-181` (same + strips `@usernames` via `URL_REGEX` at `177`, because `URL_REGEX` itself includes the mention alternative `@[A-Za-z0-9_]{3,32}` at `services.py:27`), `replace_links` `225-232`. Production callers of the helpers: the legacy orchestrator (`services.py:268-271`), the content policy's `remove_links` transform (`content_policy.py:917`), gRPC/bot surfaces via `TransformationService` (`services.py:396-412`) | LEGACY_ONLY for the orchestrator; FULLY_IMPLEMENTED for the helpers as reused components |

Note the three-way semantics split on `remove_links` inside the durable path
(`pipeline.py:1436-1457`): if `link_policy != PRESERVE_ALL` **or**
`remove_links` → the LinkManagementEngine runs (and D's
`resolve_policy` at `services.py:1001-1028` upgrades the legacy flag to
`REMOVE_ALL_URLS`, `1023-1025`); **elif** `link_replacement` →
`FilterEngine.replace_links`. A task with `remove_links=true` and a
`link_replacement` string silently ignores the replacement string, because
`1438` is an `if` not an `elif` over the flag. The cleaner's own
`remove_urls` option (CLN-URL) runs one stage earlier at `pipeline.py:1408`,
so a task with `cleaner_config.remove_urls=true` **and** a non-PRESERVE_ALL
link policy strips links twice (idempotent for removal, but the entity-offset
pass at `1445-1452` only runs for the LME hop, not the cleaner hop — the
cleaner's own removal is patched only by the later `_clamp_entities` at
`1466`, which drops/clips rather than shifts offsets).

---

## 5. Go parity (`worker/internal/engine/transform/transform.go`)

Compiled patterns (`transform.go:26-33`): `urlRegex` = the Python
`URL_REGEX` verbatim *including* the `@[A-Za-z0-9_]{3,32}` alternative;
`usernameRegex` `@[A-Za-z0-9_]{3,32}`; `hashtagRegex` `#[^\s#@]+`;
`phoneRegex` `\+?\d[\d\s\-()]{7,18}\d`; `emojiRegex` = Python
`EMOJI_REGEX`.

### 5a. Reachability — verified grep evidence

```
$ grep -rn 'transform\.New\|transform\.Engine' --include=*.go . | grep -v _test
worker/internal/engine/delivery/delivery.go:87:    transforms *transform.Engine
worker/internal/engine/delivery/delivery.go:120:       transforms: transform.New(),

$ grep -rn 'ApplyTargetOverride' --include=*.go . | grep -v _test
worker/internal/engine/transform/transform.go:56:func (e *Engine) ApplyTargetOverride(...
(no callers)

$ grep -rn 'RemoveEmojis\|Hashtags(\|Usernames(\|Tidy(\|Clamp(' --include=*.go . | grep -v _test
transform.go:127/132/136/86/102 definitions only — no callers anywhere
```

- `transform.Engine` is constructed exactly once, at `delivery.go:120`
  (`New` at `delivery.go:94-126`), and stored on the `Engine` struct at
  `delivery.go:87`.
- **`ApplyTargetOverride` has NO production caller.** Neither does
  `RemoveEmojis`, `Hashtags`, `Usernames`, `Tidy`, `Clamp`, `HasLink` or
  `FormatText` (the only cross-package reference outside `transform/` is the
  comment at `branding_stage.go:180`, which explicitly says it is *not* a
  transform). The whole `transform` package is currently dead code at
  delivery time; the worker's actual per-target override application must
  live somewhere else or not happen at all (out of scope for B — the call
  site is C/D territory, but the grep evidence for the absence is recorded
  here because the handoff §5 asks for it).
- The package doc (`transform.go:1-17`) is accurate about intent: pipeline-side
  transforms ran on ingest, and the worker should apply only the target-scoped
  override. `TargetOverride` is not referenced by any `.proto`, any generated
  pb code, or the queue/rule stores (`grep TargetOverride worker/` → only
  `transform.go`), so the struct is not yet wired to anything on the wire.

**Status:** `RemoveEmojis`/`Hashtags`/`Usernames`/`Tidy`/`Clamp` =
UNREFERENCED_HELPER; `ApplyTargetOverride` = NOT_IMPLEMENTED (written, not
reached); `urlRegex`/`emojiRegex` = UNREFERENCED_HELPER (no caller in
`worker/` outside the dead functions).

### 5b. Python vs Go regex comparison (verifier-executed)

| Concern | Python (cleaner / FilterEngine) | Go | Divergence |
|---|---|---|---|
| @mention | cleaner `_CLEANER_MENTION_RE` `@[A-Za-z0-9_]{4,64}` (`entities.py:101`); FilterEngine/`URL_REGEX` `@[A-Za-z0-9_]{3,32}` (`services.py:27`, `187`) | `@[A-Za-z0-9_]{3,32}` (`transform.go:28`) | Yes — see parity P1 |
| #hashtag | cleaner `#\w+` (`entities.py:102`); FilterEngine `#\S+` (`services.py:195`) | `#[^\s#@]+` (`transform.go:29`) | Yes — P2 |
| phone | cleaner candidate+callback, 8-15 digits, `+` or ≥10 digits + separator (`entities.py:111-131`); FilterEngine `(?:\+?\d{1,4}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}` (`services.py:203`) | `\+?\d[\d\s\-()]{7,18}\d` (`transform.go:30`) | Yes — P3 |
| emoji | identical five alternatives | identical | No divergence |
| URL | `https?://[^\s]+|www\.[^\s]+|(?:https?://)?(?:t(?:elegram)?\.(?:me|dog))/[^\s]+` | identical (plus the mention alternative, matching `URL_REGEX` not `_CLEANER_URL_RE`) | No divergence vs `URL_REGEX` |

---

## 6. Parity findings

Each finding: fixture → Python output → Go output → expected → severity →
recommended separate fix PR. "Python" is the durable cleaner path unless noted.

### P1 — mention regex `{4,64}` vs `{3,32}` (CLN-MENTION / UNI divergence)

- Fixture: `@abc` · Python cleaner: `'@abc'` (not matched: 3 chars < 4) · Go
  `usernameRegex`: `''` (removed) · FilterEngine legacy helper: `''`
  (`{3,32}` at `services.py:187`).
- Fixture: `@`+35 chars · Python: `''` (removed, ≤64) · Go: trailing 3 chars
  left (`{3,32}` matches the first 32) · FilterEngine legacy: same as Go.
- Expected: one definition. Today the *same repo* has three lengths: 4-64
  (cleaner), 3-32 (FilterEngine helpers and `URL_REGEX`'s mention
  alternative), 3-32 (Go). Two Python tasks with identical options can differ
  depending on which path edited their text, and a 3-char handle is stripped
  by the legacy path but kept by the cleaner.
- Severity: **MEDIUM** (silent content divergence between two live Python
  paths; the Go side is dead code today).
- Fix PR (separate): unify on one length constant in
  `core/domain/entities.py:101` + `core/domain/services.py:27`, then port.

### P2 — hashtag `#\w+` vs `#\S+` vs `#[^\s#@]+`

- Fixture `#hello-world` · Python cleaner `#\w+`: `'-world'` (word chars only)
  · Python FilterEngine `#\S+`: `''` · Go: `''`.
- Fixture `#hello@x` · cleaner: `'@x'` · FilterEngine: `''` · Go: `'@x'`.
- Fixture `#привет` · all three: `''` (`\w` is Unicode-aware in Python 3
  `re`; confirmed).
- Expected: one definition. The cleaner truncates a tag mid-token and leaves
  punctuation behind; the legacy path removes the whole token.
- Severity: **MEDIUM**.
- Fix PR (separate): pick `#[^\s#@]+` (or `#\S+`) in
  `core/domain/entities.py:102`, keep legacy parity in mind.

### P3 — phone number regexes diverge in both directions

- Fixture `id 1234567890` (bare 10-digit id) · Python cleaner: unchanged
  (deliberate, comment `entities.py:108-110`: "A bare 10-digit product id is
  deliberately left alone") · Go `phoneRegex`: `'id '` (removed, no digit
  boundary guard — `\+?\d[\d\s\-()]{7,18}\d` matches *inside* a longer digit
  run: `'1234567890'` → `''` from offset 0 and again from offset 2…) ·
  FilterEngine legacy: `'id '` (removed).
- Fixture `ip 192.168.1.100` · Python cleaner: `'ip '` (removed — dots are
  separators and `192.168.1.100` has 12 digits) · Go: unchanged (`.` is not
  in Go's separator class) · FilterEngine legacy: unchanged.
- Fixture `two 1234 5678 90123 678` · cleaner: unchanged (20 digits > 15 cap)
  · Go: `'two '` (matches across spaces) · legacy: `'two 3 678'` (partial
  match).
- Fixture `+98 21 8866 9988` · all three: removed.
- Expected: one definition of "phone number". The cleaner is deliberately the
  most conservative; Go is the most aggressive and will eat product ids and
  digit-heavy text without separators.
- Severity: **HIGH** if/when the Go path is wired up; **MEDIUM** now (the
  Python cleaner vs the Python legacy helper already disagree, and the legacy
  helper is live on the edit-sync path).
- Fix PR (separate): port `_strip_phone_numbers`'s candidate+callback (or at
  least the digit-boundary lookarounds and the 8-15 digit cap) to
  `transform.go:30`, and reconcile `services.py:203`.

### P4 — emoji regex leaves ZWJ and keycap fragments (UNI-ZWJ / UNI-KEYCAP)

- Fixture `👨‍👩‍👧` → Python: `'\u200d\u200d'` (two invisible joiners, which
  Telegram may render as nothing or as a stray). Go: identical.
- Fixture `5️⃣` → Python: `'5⃣'` (`5` + U+20E3 combining enclosing keycap,
  a dangling combining mark). Go: identical.
- Fixture `𒀀abc` (Cuneiform) → `abc` — non-emoji SMP text is destroyed.
- Expected: an emoji filter should remove complete emoji grapheme clusters
  (or at least not leave half-broken ones) and should not delete non-emoji
  scripts. The whole-supplementary-plane alternative is the root cause of
  both.
- Severity: **MEDIUM** (correctness for affected scripts: Sumerian/Cuneiform,
  Deseret, math alphanumerics, tag characters; cosmetic breakage of ZWJ
  sequences and keycaps). Same in Python and Go — the ports agree, the
  *pattern* is wrong.
- Fix PR (separate): replace the five alternatives with a grapheme-cluster
  aware strip (Unicode emoji property or `re`/`regex` `\p{Emoji_Presentation}`
  + ZWJ sequence handling) in `entities.py:103` and `services.py:31` together,
  then port to `transform.go:33`. Must keep `❤`-without-VS behavior stable
  (there are tests around the misc-symbols range).

### P5 — cleaner whitespace collapse differs from Go `Tidy` and Python `_tidy`

- Python cleaner (`entities.py:2402-2406`): `[ \t]{2,}` → `" "` **always**;
  `remove_empty_lines` → `\n[ \t]*\n…` → `"\n"`; then **always**
  `\n{3,}` → `"\n\n"`; then `.strip()`.
- Go `Tidy` (`transform.go:86-97`): CRLF→LF, loop-collapse `\n\n\n`→`\n\n`,
  `TrimSpace` only. **No space collapsing at all.**
- Python `_tidy` (`content_policy.py:972-976`, used by the strip-words path)
  : `[ \t]{2,}` → `" "` and `[ \t]+\n` → `"\n"`, then strip-empty. Collapses
  trailing spaces before newlines, which *neither* the cleaner nor Go does.
- Observed: cleaner on `"a  x  b   c\n\n\n\nd"` (pattern `x`) →
  `'a b c\n\nd'` (3 spaces collapsed, 4 newlines → 2). Go `Tidy` on the same
  after removing `x` → `"a  b   c\n\nd"` (spaces untouched).
- Expected: one tidying convention. Three implementations exist and disagree
  about spaces; the Go one is the odd one out and is also unreferenced.
- Severity: **LOW** (cosmetic) / **MEDIUM** for cross-engine parity when the
  worker path goes live.
- Fix PR (separate): align `transform.go:86` with the cleaner's collapse
  rules, or factor one shared `tidy` spec.

### P6 — emoji removal can run twice in the durable pipeline (UNI-DUP)

- Fixture: task with `cleaner_config={"remove_emojis": true}` AND
  `remove_emojis` flag true (both are written by different surfaces: the web
  cleaner tab writes `cleaner_config`, `pro_bot_ui.py:6706` writes the flag).
- Python durable path: `CleanerRule.clean` strips emoji at `pipeline.py:1413`,
  then `pipeline.py:1460-1461` strips them again with `FilterEngine.remove_emojis`.
- Go: no equivalent (dead code).
- Expected: one strip per message. Today it is idempotent so no content
  difference, but the two config stores can disagree (flag on, cleaner
  `remove_emojis` off → emoji still stripped via the flag path, silently
  contradicting the cleaner config the UI shows).
- Severity: **MEDIUM** (config-store ambiguity, not a content bug).
- Fix PR (separate): drop one of the two paths — the flag path at
  `pipeline.py:1460` is the redundant one given `cleaner_enabled` derives from
  it, but removing it changes the "flag without master switch" behavior pinned
  by `test_an_option_on_without_the_master_switch_still_cleans`
  (`test_cleaner_options_end_to_end.py:317`); the safer fix is to make
  `build_cleaner()` treat a true flag as a config option (it already does via
  `entities.py:1042`) and have `pipeline.py:1460` skip when a cleaner ran.

### P7 — cleaner `apply_to_caption` cannot turn caption cleaning off

- Fixture: media message, caption `"😀 pic"`, task with
  `cleaner_config={"remove_emojis": true, "apply_to_caption": false}`.
- Python durable: `pipeline.py:1410` calls
  `cleaner.clean(payload.effective_text)`, and `effective_text` returns the
  caption for media (`entities.py:2498-2502`). Nothing reads
  `apply_to_caption`. Output: ` pic` — caption cleaned despite the flag
  saying not to.
- Expected: `apply_to_caption: false` keeps the caption intact (it is
  documented at `entities.py:2352` as "also cleans media captions" — the
  default True is the documented behavior, but the False case is
  unimplemented in every delivery path).
- Severity: **MEDIUM** (a documented option that silently does nothing; the
  UI offers it as a toggle, `TaskOverviewModal.tsx:1394`).
- Fix PR (separate): in `pipeline.py:1410-1419` and `use_cases.py:948-957`,
  gate the caption case on `cleaner.apply_to_caption` (clean
  `payload.text`/`caption` separately instead of `effective_text`).

### P8 — `remove_links` + `link_replacement` conflict silently

- Fixture: task with `remove_links=True` (field) and
  `link_replacement="({LINK})"`. Text `"see https://t.me/x"`.
- Python durable: `pipeline.py:1438` is true → `LinkManagementEngine`
  (removes, per D's `resolve_policy` upgrade at `services.py:1023-1025`); the
  `elif` at `1455` never runs, so `link_replacement` is silently ignored. The
  legacy orchestrator does the converse priority (`services.py:268-271`:
  replacement **wins** over remove_links). Edit-sync and fresh-forward
  disagree.
- Expected: either replacement wins or an explicit validation error; not
  path-dependent silence.
- Severity: **MEDIUM**.
- Fix PR (separate): decide precedence and encode it in both paths, or
  validate the combination at save time.

---

## 7. Fixtures proposed

(coordinator builds the JSON; one line each)

- `CLN-FULL-ORDER` — every cleaner option on, one input exercising each step,
  pins the `clean()` execution order and the unconditional whitespace
  collapses.
- `CLN-PHONE-BARE-ID` — `"id 1234567890"`: cleaner must keep, Go
  `phoneRegex` would remove (P3).
- `CLN-PHONE-IP` — `"ip 192.168.1.100"`: cleaner removes, Go keeps (P3).
- `CLN-PHONE-TOO-LONG` — `"two 1234 5678 90123 678"`: cleaner keeps (>15
  digits), Go removes across spaces (P3).
- `CLN-MENTION-3` — `"msg @abc end"`: cleaner keeps `@abc`, legacy
  `{3,32}` removes (P1).
- `CLN-MENTION-35` — `@`+35 chars: cleaner removes all, Go/legacy leave 3
  chars (P1).
- `CLN-HASHTAG-TRAILING` — `"#hello-world"`: cleaner leaves `-world`, legacy
  and Go remove the token (P2).
- `CLN-HASHTAG-UNICODE` — `"#سلام"`: all three remove (pin that `\w` is
  Unicode-aware, do not regress on a future ASCII-only port) (P2).
- `UNI-ZWJ-FAMILY` — `"👨‍👩‍👧"` → `'\u200d\u200d'`: pins that ZWJ survives and
  proves the broken-output defect (P4).
- `UNI-KEYCAP` — `"5️⃣"` → `'5⃣'`: pins the dangling combining keycap (P4).
- `UNI-CUNEIFORM` — `"𒀀abc"` → `'abc'`: pins non-emoji SMP collateral
  damage (P4).
- `UNI-FLAG` — `"🇺🇸"` → `''`: regional indicators are removed (documented
  positive case, must survive a fix).
- `UNI-ARROW` — `"← → ↑"` unchanged: pin that 2190-21FF is out of scope (P4).
- `RPL-PRIORITY-ORDER` — `{"#": "##", "##": "x"}` both orderings, plus
  priority-flipped variants: pins the sequential + stable-priority semantics
  of §3a.
- `RPL-SMART-CASE` — `{"Discount": "Sale"}` on mixed case: exact-case replace
  applies to all occurrences once the exact form is present (pin the
  non-obvious "exact present wins" semantics).
- `RPL-LITERAL-BACKSLASH` — `{"a": "\\1"}` → literal `\1` (regex entries only
  get backreferences).
- `RPL-INVALID-REGEX` — `{"re:(unclosed": "x"}` → text unchanged.
- `CLN-EMPTY-AFTER-CLEAN` — message whose only content is a stripped link →
  drop with reason `empty_after_clean` (pins `pipeline.py:1468-1486`).
- `CLN-MASTER-OFF-LEGACY` — `cleaner_enabled: false` + legacy flag on → no
  cleaning (pins the explicit-off-wins semantics of §1a).
- `CLN-REMOVE-LINKS-METADATA-ONLY` — raw row with `remove_links` only in
  `metadata` → no cleaning (pins the field/metadata asymmetry of §1a).
- `CLN-CAPTION-FLAG-OFF` — media + `apply_to_caption: false` + emoji
  cleaner → caption still cleaned (pins P7 as a known defect).
- `CLN-LINK-REPL-IGNORED` — `remove_links` + `link_replacement` → replacement
  ignored on the durable path (pins P8).

---

## 8. Unknowns / NOT_VERIFIED

1. **Whether any production path sets `cleaner_config` from Go-side
   delivery.** The `transform` package has no caller at all (§5a); I verified
   the absence with repo-wide greps, but a negative grep over
   `worker/` cannot prove a caller outside the repo (e.g. a separate
   deployment tool). NOT_VERIFIED.
2. **`apply_to_caption` history.** Its default-True and its presence in
   `to_draft`/`apply_draft` (`entities.py:2124`) suggest it once gated caption
   cleaning; I found no git archaeology permitted (audit-only, no
   `git log` archaeology was needed) and no current reader. Whether an older
   pipeline version honored it is NOT_VERIFIED.
3. **The exact behavior of `FilterEngine.remove_urls` on markdown links with
   nested brackets** (`[a](http://x "t")`) — the character-class
   `[^\)]+` at `services.py:163/165` handles the common case; exhaustive
   Markdown edge cases were not exercised. NOT_VERIFIED (low value).
4. **Bot panel writing `cleaner_config`.** `pro_bot_ui.py` writes the legacy
   flags (verified at `6706`, `9318`); whether it also writes
   `cleaner_config` blobs (and with which key set) was greped only for
   `cleaner_config` hits — none outside entities/tests/web — so the bot
   surface's cleaner blob shape is NOT_VERIFIED.
5. **Go `Tidy`'s loop vs `strings.Regexp`**: Go's
   `for strings.Contains(text, "\n\n\n")` is O(n²) worst case on pathological
   input; not a behavior finding, noted only as a future port hazard.
6. **`UNI-DUP` interaction with `cleaner_enabled=False`.** When the master
   switch is off but the flag `remove_emojis` is true, `pipeline.py:1460`
   still strips emoji (test at
   `test_cleaner_options_end_to_end.py:317` pins this). I documented it as
   intended legacy behavior per the test; the product intent is coordinator's
   call, not mine.
