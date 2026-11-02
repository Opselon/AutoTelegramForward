# Agent A — Filters & Content Policies

## Mission
Stage 4.0 discovery-only audit of AutoTelegramForward (repo: /root/AutoTelegramForward, branch `audit/complete-business-logic-inventory`, baseline master `35ee530`). You are one of four parallel agents. **Audit only — do NOT rewrite engines, change schemas, or change runtime behavior.**

## Your scope (feature prefixes)
`FLT-*` (FilterRule), `POL-*` (ContentPolicy), `MED-*` (media filtering), `RT-*` (rule matching only), `SET-*` (setting registry semantics).

## Hard rules
- Cite `file:line` for every behavioral claim.
- Never edit any file outside `docs/behavior-audit/_handoff/a-filters-content-policies.md`.
- Never edit `docs/feature_catalog.json` or another agent's handoff.
- Never write Go/Python production code; never `git commit`/`git push`.
- `FULLY_IMPLEMENTED` requires a pasted production call chain reaching the code.

## Required deliverable
Write ONE file: `docs/behavior-audit/_handoff/a-filters-content-policies.md` with these sections:

### 1. SETTING_SPECS inventory (SET-*)
All 76 keys in `core/domain/rule_settings.py:96`. For each: key, kind, default, group, inheritable, storage (first-class column vs `metadata[...]`), the entity property that reads it (`core/domain/entities.py`), runtime reader (exact engine+function), execution phase, Go parity (COMPLETE/PARTIAL/ABSENT/UNVERIFIED), known gap. Note the deliberate exclusions documented in the module docstring (first-class fields like `forward_mode`, `content_mode`, `delay_seconds`).

### 2. FilterRule evaluation contract (FLT-*)
Read `FilterEngine.evaluate` at `core/domain/services.py:37-190`. Document the EXACT evaluation order (service messages → disabled filter → blocked media → allowed media → block_links → min/max length → blacklist → whitelist → legacy regex_patterns compat → regex_block → regex_require). For each of the ~12 settings: meaning, default, precedence, AND/OR semantics, drop reason code, legacy compat. Cover: `is_enabled`, `drop_service_messages`, `allowed_media_types`, `blocked_media_types`, `min_message_length`, `max_message_length`, `block_links`, `blacklist_keywords`, `whitelist_keywords`, `regex_block`, `regex_require`, `regex_patterns` (legacy). Note regex fails CLOSED (invalid pattern → DROP) at services.py:138-145 and 163-170.

### 3. ContentPolicy contract (POL-*)
Read `core/domain/content_policy.py` (class at 691). Document: master switch `enabled` semantics (None=auto, True=requires has_rules, False=off), per-list switches `blacklist_off`/`whitelist_off` (v30), `whitelist_mode` any/all/phrase (evaluate() at 823-906), `case_sensitive`/`word_boundary`/`normalize` match flags, regex_block/regex_require, min/max length, allowed/blocked media, block_links. Then `transform()` at 907 (remove_links→strip_words→replacements). Document `policy_chain()` at `core/domain/entities.py:1380` (task + source override + target override, additive-only). Also: `contains_word`, `fold_text` (Arabic/Persian folding), `compile_user_regex`, `bounded_search`, `_hostile_pattern` (ReDoS guard) — all in content_policy.py top section.

### 4. Blacklist/whitelist distinct implementations
This is critical — there are at least 6: (1) `FilterRule.blacklist_keywords`, (2) `FilterRule.whitelist_keywords`, (3) `ForwardRule.blacklist`/`whitelist` (entities.py:1242/1258 — keyword reply keywords), (4) `ForwardRule.blacklist_users`/`whitelist_users` (entities.py:1274/1287), (5) `ContentPolicy.blacklist_words`/`whitelist_words`, (6) source/target policy overrides. For each: where stored, what reads it, whether it can reject a message. Also check `blacklist_words`/`whitelist_words` on the entity (entities.py:989/997) vs the policy blob.

### 5. Media filtering (MED-*)
`core/domain/media_filter.py` — all `MEDIA_TYPES` (20), the 5 `_BLOCK_FLAGS` with their type coverage (video→video+video_note etc.), allow-list precedence over block flags, `link_block_reason`. Cross-reference `FilterRule.blocked_media_types` vs rule-level `allowed_media_types` (entities.py:407).

### 6. Rule matching / SmartRoutingEngine (RT-*)
`core/domain/services.py:415-678`: `SmartRoutingEngine`, `RoutingPolicy`, `matching_rules` logic, `message_category` (ALL/VIP/REGULAR/CHANNEL_POST/USER_MESSAGE), `detection_criteria`, `multi_route`, `priority`/`execution_order`, loop prevention (`is_loop`), `matches_source` (entities.py:1758). Also `ForwardRule.source_chat_ids`, `text_contains`, `regex_pattern`, `match_mode` (entities.py:1664+).

### 7. Go parity
Read `worker/internal/engine/filter/filter.go` fully (~480 lines) and `worker/internal/engine/transform/transform.go`. Document what the Go filter does/doesn't vs Python `FilterEngine.evaluate` — check evaluation order, `containsWord`/`foldText`/`collapseWS` (Go lines 298-470), `compileUserRegex` (243), `boundedMatch` (259). The Go filter is called at `worker/internal/engine/delivery/delivery.go:212`. Note: Go does NOT re-evaluate ContentPolicy at delivery (comment at delivery.go:193-199).

### 8. Parity findings + fixtures + unknowns
Each finding: minimal input fixture, Python output, Go output, expected behavior, severity, recommended separate fix. Fixture proposals: scenario id + one-line description.

## Starting points
`core/domain/rule_settings.py`, `core/domain/entities.py`, `core/domain/services.py` (37-678), `core/domain/content_policy.py`, `core/domain/media_filter.py`, `worker/internal/engine/filter/filter.go`, `worker/internal/engine/delivery/delivery.go`, existing tests in `core/tests/`.
