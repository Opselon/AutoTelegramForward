# Behavior Contract — what each setting actually does

**Baseline:** master `35ee530cc80cd17d1ac7d702c4e5f5ea8024889a`. Audit-only.
This document is the behavior contract for migration. Full machine-readable inventories:
`docs/behavior-audit/settings-inventory.json` (89 settings) and
`docs/behavior-audit/feature-inventory.json` (64 features).

---

## FilterRule.evaluate — evaluation order is part of the contract

`core/domain/services.py:37-190` and `worker/internal/engine/filter/filter.go:84-222`.

| Step | Check | Default | Drop reason |
|---|---|---|---|
| 1 | service message | `drop_service_messages=true` | `service_message_dropped` |
| — | `is_enabled` off | `true` | allow (`filter_disabled`) |
| 2 | `blocked_media_types` | `[]` | `media_blocked:<type>` |
| 2b | blocked contains `link` + text link | — | `link_blocked` |
| 3 | `allowed_media_types` | `[]` (allow-all) | `media_not_allowed:<type>` |
| 4 | `block_links` | `false` | `link_blocked` |
| 5 | `min_message_length` | `0` (no bound) | `too_short` |
| 5b | `max_message_length` | `0` (no bound) | `too_long` |
| 6 | `blacklist_keywords` | `[]` | `blacklist_keyword` |
| 7 | `whitelist_keywords` | `[]` (allow-all) | `whitelist_mismatch` |
| 8 | `regex_block` (fails closed) | `[]` | `regex_block:<pattern>` / `invalid_regex_block:` |
| 9 | `regex_require` (require-any, fails closed) | `[]` | `regex_mismatch` / `invalid_regex_require:` |

- An **empty allow-list means allow-all, never deny-all** — both engines.
- `regex_patterns` (legacy) becomes `rx_block` when blacklist-only, `rx_require` otherwise.
- Go evaluates `block_links` **last** (F-11, MEDIUM) and never receives it from the store
  (F-12, HIGH). Both are recorded; neither is fixed here.
- Matching is **substring** (`word_boundary=False`) via `content_policy.contains_word` —
  `5x` inside `a5x` matches. Unicode folding (Arabic/Persian Yeh/Kaf) applies.

## ContentPolicy.evaluate / transform

`core/domain/content_policy.py:691-1006`. A different feature from FilterRule: it lives in
`metadata["content_policy"]`, supports source/target overrides, and both decides **and**
rewrites.

- `enabled`: `None` = auto (active iff it has rules), `True` = active iff `has_rules`,
  `False` = off. An unset policy with rules **is** active.
- `blacklist_off` / `whitelist_off` (v30): park a list without deleting its words; a parked
  list also parks its regex list (`regex_block` is blacklist-side, `regex_require`
  whitelist-side).
- `whitelist_mode`: `any` (default) / `all` (every word must be present) /
  `phrase` (words joined by a single space, matched against whitespace-collapsed text).
- Match flags: `case_sensitive` (default false), `word_boundary` (default **true**,
  the opposite of FilterRule), `normalize` (default true — Arabic/Persian folding).
- `transform()` order: `remove_links` (via `FilterEngine.remove_urls`) → `strip_words`
  (boundary-aware) → `replacements` (**single pass**, deliberately the opposite of the
  task-level replace engine's sequential semantics).
- `policy_chain` (`entities.py:1380`): task + source override + target override.
  **Additive-only**: nothing in a chain can relax the task's own rules.

## Cleaner — 9 options, fixed order

`ForwardRule.build_cleaner()` (`entities.py:808-880`) → `CleanerRule.clean()`
(`entities.py:2380-2405`).

Order: `patterns` → `urls` → `mentions` → `hashtags` → `emoji` → `phones` → unconditional
`[ \t]{2,}` collapse → `remove_empty_lines` → unconditional `\n{3,}` collapse → `.strip()`.

- `cleaner_enabled` derives `True` when any legacy flag is set, so an old task is never
  silently un-cleaned.
- `cleaner_config` key wins over the legacy flag; `clean_custom_words` is
  dedup-appended to `patterns`.
- `apply_to_caption=False` **cannot disable caption cleaning** (F-09): the pipeline feeds
  `payload.effective_text`, which is the caption for media messages (`entities.py:2498`).
- Master switch off + no options → `build_cleaner()` returns `None` → nothing runs, and
  the retained options are preserved verbatim.

## Replace engine

`core/domain/text_replace.py`. Storage compatibility: plain legacy string, structured
dict, rid-keyed, and `metadata["replacements"]` — all normalized by `entry_from_value`.

- Modes: `literal` (default) / `regex`; flags `i c m s x w` (whole-word).
- **Smart case** (legacy behavior, must be preserved): exact-case `str.replace` first;
  case-insensitive `re.sub` only when the exact form is absent.
- Priority: higher runs first; **equal priorities keep insertion order** (stable sort).
  `{"#":"##"}, {"##":"x"}` in insertion order yields `x`; reversed yields `##`.
- Literal entries escape backslashes (`\1` renders literally); regex entries use `\1` as a
  backreference.
- Invalid pattern → skip the entry, log, text unchanged. Never raises.
- Applied exactly **once** in the durable path, after AI and before header/footer
  (`pipeline.py:1543`), once on the reply path (`use_cases.py:978`).

## Link management — 9 distinct authorities

`remove_links` (cleaner toggle) · `link_replacement` · `link_policy` (8-value enum) ·
`domain_allowlist` / `domain_blocklist` · `link_rewrite_map` ·
`blocked_media_types=link` · `FilterRule.block_links` · ContentPolicy link logic.

- `resolve_policy` fails **closed** to `PRESERVE_ALL` on an unknown value; a `remove_links`
  flag upgrades `PRESERVE_ALL` → `REMOVE_ALL_URLS` (`services.py:1001-1028`).
- `remove_links=true` silently ignores `link_replacement` on the durable path (F-04).
- `filter_inline_keyboard` has **zero production callers** (F-16): link policy strips text
  but leaves inline URL buttons untouched.
- `link_policy` / `domain_*` are absent from `SETTING_SPECS`, so the validated settings
  API cannot write them.
- Go has no link policy surface at all (F-18) — the whole engine must port in Stage 4.4.

## Advanced settings

`AdvancedSettingsEngine` (`services.py:1409-1567`), applied at `pipeline.py:1558`.

- Order: watermark → link_buttons (add-on) → affiliate_kit (add-on). Watermark anchors the
  very end of the body, after header/footer; capped at 200 chars; deduped by substring.
- Add-on keys are checked server-side via `is_addon_enabled` — a client-supplied
  entitlement flag never grants a premium transform.
- **Header placeholders never run on the durable path** (F-15, HIGH): `HeaderFormatter`
  is only reachable from the legacy `transform_text` path (edit-sync, gRPC preview).
  `{source_link}` reaches the target literally.
- Bounds disagree between layers (F-17): `auto_delete_seconds` registry max 604800 vs
  engine 31_536_000; `forward_delay_seconds` registry 86400 vs Go API 3600.

## Routing / FEN / dedup / keyword reply

- `matching_rules`: per-owner single match; `multi_route` lets subsequent rules also run.
- Loop prevention is a **per-target skip**, never a message drop (`pipeline.py:1162-1176`).
- `forward_every_n` runs **after all filters** (F-05): filtered messages consume no slot,
  albums consume one, and N=1 writes nothing (byte-for-byte legacy behavior).
- Content dedup: rule-scoped body+media-kind fingerprint; the claim is atomic
  (`UNIQUE(rule_id, content_hash)`) and **released** when delivery is rejected — the
  comments at `pipeline.py:860+` document a past silent-drop bug this prevents.
- `dedupe_policy` is stored by four layers and read by none (F-01).
- Keyword reply: `classify_keyword_trigger` reads the **source** text
  (`pipeline.py:1304`, `payload.effective_text or ctx.transformed_text` — the fallback is
  never reached), enqueues a secondary `job_kind="keyword_reply"` job that never drops the
  forward.

## Failure policy (the contract that makes the system trustworthy)

- Every transform stage is total: a failure degrades to the untransformed text, never a drop.
- The only drop inside transforms is `empty_after_clean`.
- Regex filters fail closed; invalid replace patterns are skipped.
- Session time-limited messages are **deferred**, not dropped.
- The delivery fence is claimed only after preflight, and a fence-store failure **refuses**
  to dispatch rather than sending unprotected.

## Settings with no engine reader (verified)

`debug_mode`, `process_raw_text`, `feature_group`, `sender_type`, `translate_lang`,
`blacklist_users`, `whitelist_users`, `news_digest`, `button_to_text`, plus
`dedupe_policy` (F-01) and the UI-only rows — see `settings-inventory.json` for the per-key
verdict.
