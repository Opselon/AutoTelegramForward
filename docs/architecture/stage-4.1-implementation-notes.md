# Stage 4.1 Implementation — Engineering Notes

**Branch:** `feat/stage-4.1-canonical-content-policy`
**Stacked on:** PR #289 (`audit/complete-business-logic-inventory`, HEAD `a817c59`)
**Baseline:** master `35ee530cc80cd17d1ac7d702c4e5f5ea8024889a`

This file records what was implemented, how it was verified, and the ownership
boundaries Stage 4.2+ must respect. It is the engineering record, not a plan.

---

## 1. F-12 resolution (verified, verdict A)

The Stage 4.0 audit's verdict was confirmed against every persistence layer and
every control surface before any code was written:

- `block_links` is a **task-level** setting everywhere it is exposed: the bot's
  Filters screen (`pro_bot_ui.py:6804`, the `rfl_lnk:` callback on
  `_owned_rule`, which is the `IForwardRuleRepository`), the Go API settings
  allowlist (`api/internal/handlers/handlers.go:491`), and Flutter
  (`mobile/lib/features/rules/rule_settings_sheet.dart:94`).
- Python enforces it at `core/application/pipeline.py:957` through
  `media_filter.link_block_reason(rule, text)`, which reads
  `ForwardRule.block_links` — a metadata key (`core/domain/entities.py:678`).
- The Python `FilterRule` dataclass (`core/domain/entities.py:2312-2333`) has
  **no** `block_links` field, so `services.py:79`'s
  `getattr(filter_rule, "block_links", False)` is dead by construction.
- SQLite's `filter_rules` table has no `block_links` column in any migration
  (the table is rebuilt whole at v1, v11 and v30 — never `ALTER`ed), and
  PostgreSQL has no `filter_rules` table at all.

**Verdict (A): an existing intended feature missing from the Go projection.**
The fix projects the task metadata flag the Go rulestore already loads.
**No database migration, no new column, no new setting, no proto change.**

## 2. What was implemented

### 2.1 `worker/internal/domain/policy` (new package)

- **`policy.go`** — `ContentPolicy` evaluator, a behavioral port of
  `core/domain/content_policy.py:691`. `enabled` tri-state
  (nil=auto/has_rules, true=active iff has_rules, false=off), per-list
  `blacklist_off`/`whitelist_off` switches that park a list without deleting
  its words, `whitelist_mode` any/all/phrase, regex_block/regex_require that
  **fail closed** on an unusable pattern, and the additive `Chain`
  (task → source → target; first deny wins, nothing can relax the task policy).
  Reason strings match Python exactly (`whitelist_missing:<words>`,
  `regex_block:<pattern>`, `invalid_regex_block:<pattern>`, …).
- **`matcher.go`** — the Persian/Arabic-aware matcher. NFC normalization,
  invisible-character stripping (`U+064B-0652`, `U+0670`, `U+0640`,
  `U+200B-200F`, `U+202A-202E`, `U+FEFF`), the Persian/Arabic letter-variant
  fold map and variant classes (yeh, kaf, alef, heh, waw), and the
  needle-pattern builder where each folded character becomes a variant class
  and a space becomes `\s+`.
  Includes the folded-needle pre-filter from `content_policy.py:_folded_needles`
  so a 500-word list costs one C-level scan per word, not 500 regex passes.
- **`link.go`** — the **canonical link classifier** (fixes F-14). One
  `HasLink` predicate plus `Classify` returning the ordered link classes
  (HTTP, HTTPS, t.me, telegram.me, tg:// deep link, plain-text www., …).
  It follows Python's `has_link`/`_RE_LINK`, which pins the two-sided
  divergence: `tg://` **is** a link, a bare `@username` is **not**.
- **`bench_test.go`** — hot-path benchmarks (see §4).

### 2.2 `worker/internal/engine/filter/fire.go` — codepoint length fix

Python's `len(text)` counts code points; Go's `len(string)` counts UTF-8 bytes.
The two length gates used the byte count, so for
`max_message_length = 9` Python allowed `"سلام دنیا"` (9 code points) while
Go dropped it (17 bytes). Both gates now use `utf8.RuneCountInString`, with a
regression test matrix covering Persian, Arabic, astral emoji and mixed
scripts plus the English boundaries that must not regress.

### 2.3 `worker/internal/rulestore` — F-12 projection

`metaBool` joins `metaStr`/`metaInt64`, and the projection the task metadata
blob already decodes resolves `BlockLinks` on `ForwardRule`, exactly the way
`delivery_transport` and `album_mode` are resolved. Rows without the key
project to `false` (the pre-feature default on every legacy row).

### 2.4 `worker/internal/engine/delivery` — production integration

The task-level `block_links` gate runs in the same pre-flight as the attached
`FilterRule` re-evaluation, on the payload's baked text, **before the fence
claim**, so a permanent drop never leaves an unresolved claim:
`MarkFailed("Filter: link_blocked")`. It calls the canonical
`policy.HasLink`, not the filter package's divergent `urlRegex`.

## 3. Production-reach honesty (Stage 4.1 gate §12)

- **FilterRule (filter package):** has a real production call site at
  `delivery.go:212`, unchanged. The length fix lands *in* that call path.
- **block_links (F-12):** has a real production call site at
  `delivery.go:238`, in the same pre-flight.
- **`policy.HasLink`:** has a real production call site at `delivery.go:238`.
- **`ContentPolicy` evaluator:** **no production caller yet, by design.**
  The pipeline runs the task and source policies on the *source* text at
  ingest, and only the target-scoped override on the transformed text at
  enqueue (`pipeline.py:1186-1196`, with the historical silent-drop bug that
  motivated the split). The job payload carries the *transformed* text, not
  the policy context, so the Go ContentPolicy engine cannot be wired into
  `delivery.go` without either (a) persisting the policy blob onto the job —
  Stage 4.5's ingestion-transfer scope — or (b) re-deciding the task policy on
  the transformed text, which is the documented historical bug. It is
  therefore shipped as a **tested, parity-differential-verified engine
  awaiting its Stage 4.5 integration point**, not as a completed transfer.

## 4. Benchmarks (measured, AMD EPYC 9575F, `-benchmem`)

| Benchmark | ns/op | B/op | allocs |
|---|---|---|---|
| EvaluatePolicy/SmallASCII | 3,876 | 513 | 10 |
| EvaluatePolicy/SmallPersian | 7,232 | 1,107 | 12 |
| EvaluatePolicy/Mixed | 5,449 | 834 | 10 |
| EvaluatePolicy/LargePersian (3 KB) | 459,032 | 113,192 | 12 |
| EvaluatePolicy/LargeASCII (3.5 KB) | **74,463** (was 202,224) | 40,960 | 4 |
| EvaluatePolicy/EmojiHeavy | 115,626 | 38,831 | 26 |
| EvaluatePersianKeywords (50 FA words) | 192,255 | 28,304 | 6 |
| LargeKeywordList (500 EN words) | 207,062 | 14,416 | 5 |
| Chain (task+source+target) | 146,975 | 43,158 | 17 |
| FoldText (Persian 3 KB) | 101,501 | 10,425 | 4 |
| HasLink/NoLinks (1 KB) | 43,587 | 1 | 0 |
| HasLink/OneLink | 274.9 | 0 | 0 |
| HasLink/ManyLinks | 397.1 | 0 | 0 |
| CompiledKeywordReuse (Persian) | 65,943 | 4 | 0 |

The ASCII fast path in `FoldText` cut the large-English case from 202 µs to
74 µs (2.7×) and its allocations from 10 to 4, by skipping the four stages
that are identity for pure ASCII. `HasLink` runs at zero allocation once a
link is present (RE2 finds the scheme early on the first pass) and the
compiled-keyword path is zero-allocation after the first message.

The remaining allocation cost is concentrated in the Persian large-body case,
where the fold does real work; it is not optimized further here because the
correct next step is avoiding re-folding across the chain, a Stage 4.5
concern.

## 5. Differential verification (RE2 vs Python `re`)

`matcher_test.go` runs a **differential harness** against the live Python
reference implementation — real `python3` subprocess calls into
`core/domain/content_policy.py`. This caught three genuine port defects that
unit tests alone would not have:

1. **`rightBoundary` used an optional `[^\\w]?`** — the zero-character branch
   matched "ad" inside "#advert", which Python's `(?!\\w)` rejects. The class
   is now mandatory (`[^\\w]`), accepting the documented one-character match
   offset shift in exchange for the correct decision.
2. **`FoldText` used `strings.ToLower` instead of `str.casefold()`** — so the
   needle "straße" never matched "STRASSE", where Python's casefold builds
   "strasse". `unicodeCasefold` now handles the full-width expansions
   (ß→ss, ﬀ→ff, ﬁ→fi, ﬂ→fl, ﬃ→ffi, ﬄ→ffl, İ→i̇, ı→i).
3. **`needlePattern` chained its boundary helpers** —
   `rightBoundary(leftBoundary(body))` treated the left helper's alternation
   as part of the body and duplicated it; the pattern no longer contained the
   needle. Both boundaries are now composed against the raw body.

A fourth differential failure was a **test bug**, not an engine defect:
Python's reference returns `False` for `contains_word("SPAM","SPAM",
case_sensitive=True)` because `(?i)` is always applied to the compiled
pattern. The test was asserting the opposite of the reference; it now pins
the reference's actual answer.

These are exactly the cases the brief's "do not claim compatibility without
proof" clause targets — the differential harness, not the unit tests, is the
evidence.

## 6. What was deliberately NOT done

- **No database migration.** SQLite stays at v33, PostgreSQL's schema is
  untouched, no `filter_rules` column, no proto change.
- **No user-facing setting added or renamed.** `block_links` semantics are
  preserved exactly; the Go engine now enforces what Python was already
  enforcing.
- **`worker/internal/engine/transform` stays UNREFERENCED.** It is Stage 4.2's
  scope, not 4.1's; wiring it here would silently double-apply transforms
  (F-08).
- **The five systems stay distinct.** FilterRule, ContentPolicy, the canonical
  link classifier and the filter package's own `urlRegex` are separate;
  `urlRegex` was NOT retuned to match `HasLink` because that would change an
  existing, fixture-pinned decision (F-14 documents the divergence rather
  than silently resolving it here).

## 7. Stage 4.5 dependency (the ContentPolicy integration point)

Wiring `ContentPolicy` into production requires the job to carry the policy
context, because the pipeline decides the task policy on the *source* text
while the worker sees the *transformed* text. That is the ingestion-transfer
boundary and belongs to Stage 4.5, not 4.1. The engine lands now, with
differential parity evidence, so Stage 4.5 is a wiring task rather than an
engine-writing task.
