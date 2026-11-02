# Agent S1 — Go ContentPolicy engine + link classifier

Repo /root/AutoTelegramForward, branch `feat/stage-4.1-canonical-content-policy` (stacked on audit branch a817c59). Implementation stage: Stage 4.1. **No merging/deploying. Never force-push.**

## Context (from Stage 4.0 audit, verified)
- Python reference: `core/domain/content_policy.py` (class at 691). Read it fully before coding.
- The pipeline evaluates a chain: `ForwardRule.policy_chain()` (`core/domain/entities.py:1380`) = task policy + optional source override + optional target override. Additive-only.
- Go filter package exists: `worker/internal/engine/filter/filter.go` (order: service→media→block_links→length→blacklist→whitelist→regex).
- The `worker/internal/engine/transform` package is UNREFERENCED — do NOT wire it.
- `worker/internal/engine/delivery/delivery.go:212` is the production call site for the attached filter.

## Your scope (files only you create/modify)
1. `worker/internal/domain/policy/policy.go` — the ContentPolicy domain types + evaluator
2. `worker/internal/domain/policy/policy_test.go`
3. `worker/internal/domain/policy/link.go` — the typed link classifier
4. `worker/internal/domain/policy/link_test.go`
5. `worker/internal/domain/policy/matcher.go` — Persian/Arabic-aware matching primitives (shared)

**Do not touch:** `worker/internal/engine/filter/filter.go`, `worker/internal/engine/delivery/*`, `worker/internal/rulestore/*`, anything under `core/`.

## Hard requirements

### policy.go — ContentPolicy evaluator
Port `ContentPolicy.evaluate` (`content_policy.py:823-906`) exactly:
- `enabled` tri-state: nil=auto (active iff has_rules), true=active iff has_rules, false=off. `is_active` at content_policy.py:831-835.
- Fields: blacklist_off, whitelist_off, blacklist_words, whitelist_words, whitelist_mode (any|all|phrase), block_links, remove_links, strip_words, replacements, regex_block, regex_require, min_length, max_length, allowed_media, blocked_media, case_sensitive, word_boundary, normalize, source, notes.
- evaluate() check order (content_policy.py:823-906): is_active → blocked_media → allowed_media → min_length → max_length → block_links(uses has_link) → blacklist(unless blacklist_off) → whitelist(unless whitelist_off, by mode) → regex_block(unless blacklist_off) → regex_require(unless whitelist_off). Each returns a Decision with the Python reason string.
- Reason strings MUST match Python exactly: `media_blocked:<type>`, `media_not_allowed:<type>`, `too_short:<n>`, `too_long:<n>`, `links_blocked`, `blacklist:<word>`, `whitelist_mismatch`, `whitelist_missing:<words>`, `whitelist_phrase_missing`, `regex_block:<pattern>`, `regex_mismatch`, `invalid_regex_block:<pattern>`, `invalid_regex_require:<pattern>`.
- whitelist_mode: `all` (content_policy.py:856-870) — every word must match, reason `whitelist_missing:<comma-joined first 3>`; `phrase` (:871-874) — words joined by single space matched against whitespace-collapsed text, reason `whitelist_phrase_missing`; else `any` (:875-879) — at least one, reason `whitelist_mismatch`.
- **Length MUST count Unicode code points, not UTF-8 bytes.** Python `len(body)` is codepoints. Use `utf8.RuneCountInString(body)`.
- Regex: invalid patterns FAIL CLOSED (drop with `invalid_regex_*`), mirroring content_policy.py:1042-1070. Use the same ReDoS structural guard shape as `worker/internal/engine/filter/filter.go` (reDoSNested/reDoSAlt, maxPatternLen=1000, maxMatchLen=8192).
- Policy chain: implement `Chain` (task + source + target) where every policy in the chain must allow (additive-only). A `nil` policy in the chain is skipped. First deny wins.

### link.go — typed link classifier (fixes F-14)
A typed classifier distinguishing: HTTP, HTTPS, t.me links, telegram.me/dog, tg:// deep links, plain-text URLs (www.), mentions (@username), and non-URLs.
- It must define ONE canonical predicate `HasLink(text) bool` that replaces the three conflicting predicates (Python `URL_REGEX` gate at services.py:79, `has_link`/`_RE_LINK` at content_policy.py:96/121, and Go filter's ad-hoc one).
- The canonical answer is Python's `has_link` (content_policy.py:121, `_RE_LINK` at :96): `(?:https?://|ftp://|www\.|t\.me/|telegram\.me/|telegram\.dog/|tg://)` IGNORECASE. Verify by reading those lines.
- Also expose `Classify(text) []LinkClass` returning the ordered list of link classes found, so tests and future stages can assert on categories.
- `HasLink` must NOT treat a bare `@username` as a link (that is the F-14 divergence: the FilterEngine's URL_REGEX wrongly includes `@[A-Za-z0-9_]{3,32}`).

### matcher.go — Persian/Arabic-aware matching
Port from content_policy.py:
- `fold_text` (:131): NFC normalize, strip invisibles `_RE_INVISIBLE` (U+064B-0652, U+0670, U+0640, U+200B-200F, U+202A-202E, U+FEFF), apply `_FOLD_MAP` (:63: Arabic yeh ﻱ→ﻯ, alef maksura→yeh, yeh hamza→yeh, Arabic kaf ﻙ→ﮎ, heh goals→ه, teh marbuta→ه, and the rest at :63-89 — READ THE WHOLE MAP), then casefold unless case_sensitive.
- `_VARIANT_CLASS` (:156): yeh accepts ﻯﻱﻯﺉ, kaf accepts ﮎﻙ, alef acceptsﺍﺃﺇﺁﺎ, heh acceptsﻩﺓﻰ, waw acceptsﻭﺅ.
- `_match_mode` (:164): word_boundary + first char in `#@$/` → prefix; word_boundary + any alnum → word; else substring.
- `_needle_pattern` (:176): each folded char becomes a variant class; a space becomes `\s+`; with normalize, invisible chars allowed between chars (`_INVISIBLE_RUN`); left boundary `(?<!\w)` unless substring; right boundary `(?!\w)` when word mode and last folded char is alnum.
- A `Matcher` struct cached per needle (compiled once). Provide `ContainsWord(haystack, needle, wordBoundary, normalize, caseSensitive) (bool, string)`.
- Performance: implement the `_folded_needles` pre-filter (:209-258): fold the needle, check `needle_in_folded_body` before running the full pattern, so a large keyword list is cheap. Folded body collapses `\s+`→` `. The pre-filter must never change the decision, only the cost.
- Use `golang.org/x/text/unicode/norm` for NFC (already a dep in worker/go.mod).

## Tests — use the Stage 4.0 fixtures
Load `docs/behavior-audit/fixtures.json` the same way `worker/internal/audit/fixtures_test.go` does (walk up from the test file to find repo root). Read that loader first and reuse its approach.
- Run every `ContentPolicy.evaluate` fixture (POL-*) against your engine.
- Run the link fixtures (LNK-GATE-01, LNK-GATE-02) against your classifier — these pin the F-14 divergence.
- Add Persian/Arabic folding tests: Arabic Yeh needle matches Persian Yeh text; tatweel/harakat between letters; case folding.
- Test invalid regex fails closed on BOTH regex_block and regex_require.
- Test the chain: task policy + source override (additive: override can only add constraints).
- Test empty policy (no rules) is inactive and allows.
- `go test -race` must pass.

## Report
Files changed, exact line refs for each ported function, test commands and pass/fail counts, any place where Go RE2 cannot express a Python `re` construct (document it), remaining uncertainty. Do not claim parity without a pasted test result.
