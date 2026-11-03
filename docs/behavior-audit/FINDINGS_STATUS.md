# Findings status register — Stage 4.0 finalization

**Method.** Every finding below was re-verified by the coordinator in this phase by
executing the production code path (not by reading comments) unless the status column
says otherwise. Statuses are the exact tokens required by the finalization brief.

Status vocabulary: `VERIFIED`, `PARTIALLY_VERIFIED`, `NOT_REPRODUCIBLE`,
`FIXED_IN_EXISTING_CODE`, `DOCUMENTATION_ONLY`, `NEEDS_FUTURE_IMPLEMENTATION`,
`OPEN_UNVERIFIED`.

---

## F-01 — `dedupe_policy` STORED_BUT_UNUSED — **VERIFIED**
Evidence: `core/domain/entities.py:244` defines the field; written by
`sqlite_repositories.py:388,654`, `postgres_repositories.py:378,723`, the proto
`ForwardRule.dedupe_policy` (field 30) and `backup_service.py:201,601`.
Coordinator grep across `core/` found no read in `pipeline.py`,
`durable_queue.py`, `dispatcher.py`, `services.py` or `sync_engine.py`.
Customer impact: an owner switching STRICT→DISABLED changes nothing.
Classification: **missing functionality** (the setting should do something or be pruned).
Action: decide in Stage 4.5. Not fixed here.

## F-02 — Album TTL hard-coded — **VERIFIED**
`dispatcher.py:96` `AlbumAggregator(..., ttl_seconds=2.0, ...)`;
`entities.py:410` `album_aggregation_window_seconds` (default 0.8) is never read.
Customer impact: low (album flush timing cannot be tuned per task).
Classification: **missing functionality**, wiring only.
Action: Stage 4.5 wiring task.

## F-03 — Emoji regex destroys non-emoji SMP and shreds ZWJ/keycap — **FIXED (Stage 4.4, both engines)**
Re-verified by execution in this phase on `EMOJI_REGEX`
(`core/domain/services.py:31`) and `_CLEANER_EMOJI_RE` (`entities.py:103`,
byte-identical pattern):
`👨‍👩‍👧` → `\u200d\u200d`, `5️⃣` → `5⃣`, `𒀀 cuneiform` → ` cuneiform`,
`❤️` → `` , `←` and combining marks untouched.
Customer impact: messages containing historical scripts, math alphanumerics or
complex emoji render corrupted in the target.
Classification: **real customer impact**, cross-language.

**Stage 4.4 resolution.** The shared predicate is now Unicode 15.0.0's
Extended_Pictographic property, extracted from `github.com/rivo/uniseg@v0.4.7`'s
generated table into ONE data file pair:
`worker/internal/domain/cleaning/emoji_ranges.go` (Go) and
`core/domain/emoji_table.py` (Python), both emitted by
`scripts/gen_emoji_table.py`. A Unicode update is one regeneration; the two
engines cannot drift.

The removal is sequence-aware: a whole ZWJ family, flag, skin-toned
pictograph, keycap (`<base><FE0F?><20E3>`, base included) or
variation-selector sequence goes as a unit — no dangling joiner, no orphaned
`U+20E3`, no bare digit. Non-emoji supplementary-plane scripts (cuneiform,
math alphanumerics) are not Extended_Pictographic and pass through untouched.
Arrows, Persian/Arabic and combining marks are unaffected.

`FilterEngine.remove_emojis` and `CleanerRule.clean` route through the shared
stripper on the Python side; the Go pipeline's emoji flag and the cleaner's
`remove_emojis` route through `cleaning.RemoveEmojisGrapheme`. The
UNI-EMOJI-01/02/03/04 fixtures now pin the FIXED outputs (their pre-fix values
are preserved in each fixture's notes), and 9 new differential cases in
`scripts/test_stage42_parity.py` cover ZWJ families, keycaps, flags, skin
tones, arrows and non-emoji SMP scripts (69/69 agree).

## F-04 — `remove_links`/`link_replacement` precedence differs per path — **RESOLVED for the Worker (Stage 4.2 port, re-verified 4.4)**
Reproduced by execution: durable path (`pipeline.py:1438` `if`/`elif`) with
`remove_links=True` and `link_replacement="[LINK]"` on `"visit https://a.io now"`
yields `'visit now'` — the replacement is ignored. Legacy
`FilterEngine.transform_text` (`services.py:268-271`) gives replacement precedence
and yields `'visit [LINK] now'`.
Customer impact: edits (which run the legacy path) format links differently from
the original forward.
Classification: **conflicting specifications between two Python paths**.

**Resolution for the Worker.** The divergence is Python-internal (durable vs
legacy path), not Go/Python. The Go transform pipeline implements the DURABLE
path precedence exactly — `resolvePolicy`/`if`/`else-if`
(`pipeline.go:120-137`) gives `remove_links` the win and drops the replacement,
producing `visit now` from the same input, verified by probe against the Python
`LinkManagementEngine`. That is the correct side to match: the durable path is
the authoritative one, and the legacy `transform_text` is the Python-only
preview/edit path the Worker does not inherit. The remaining task — unifying the
two PYTHON paths — stays with the Python core until the strangler switch moves.

## F-05 — `forward_every_n` runs AFTER all filters — **VERIFIED**
`pipeline.py:1036-1084` sits after `FilterEngine.evaluate` (918), media/link gates
(955) and the content-policy chain (974-1016). Filtered messages consume no slot;
albums consume one. **Intentional legacy behavior — preserve.** No action.

## F-06 — `materialize()` precedes every gate — **VERIFIED**
`pipeline.py:787`. All later gates read the bot-defaults-merged view. Additive-only
(see `effective_settings.py`), so a bot default cannot rescue a message the task's
own policy rejects. **DOCUMENTATION_ONLY** — already correct, documented in
`behavior-contract.md`.

## F-07 — Cleaner/legacy/Go regex divergence (mention/hashtag/phone) — **VERIFIED**
Re-verified by execution in this phase:
- mention: cleaner `@[A-Za-z0-9_]{4,64}` (`entities.py:101`) keeps `@abc`;
  legacy `services.py:27` and Go `transform.go:28` use `{3,32}` and strip it.
- hashtag: cleaner `#\w+` (`entities.py:102`) leaves `-world` on `#hello-world`;
  Go `#[^\s#@]+` consumes it.
- phone: cleaner's guarded candidate (`entities.py:111-131`) preserves a bare
  10-digit product id; Go `\+?\d[\d\s\-()]{7,18}\d` would eat it.
Customer impact: text is cleaned differently depending on which path handles it.
Classification: **conflicting specifications** across three engines.
Action: Stage 4.2/4.1 unify.

## F-08 — Go `transform` package UNREFERENCED at delivery time — **FIXED (Stage 4.2)**
`delivery.go:87,120` constructs `*transform.Engine`; coordinator grep confirms
`ApplyTargetOverride`, `RemoveEmojis`, `Hashtags`, `Usernames`, `Tidy`, `Clamp`,
`HasLink`, `FormatText` have no production call sites — for the *per-target
override*. Per `delivery.go:193-199` the ingest side bakes transforms into the
job payload, which was the intended Stage 3 boundary.

**Stage 4.2 resolution.** The Go Worker now owns the transform pipeline
itself (`worker/internal/domain/cleaning`), wired to the production path by
`delivery.applyTransformPipeline` under the `ATF_WORKER_TRANSFORMS`
strangler-fig switch. The transform package's per-target override is
unchanged (still the F-13-pinned divergence fixture). The
cleaner/replacement/emoji/link engines that were "unreferenced" are now the
package the worker dispatches through, with byte-for-byte parity against
the Python engines proven by `scripts/test_stage42_parity.py` (49/49
cases) and the reclassified both-class fixtures.

## F-09 — `apply_to_caption=False` cannot disable caption cleaning — **FIXED (Stage 4.4, Go side)**

Original finding (still accurate for the Python path):
Reproduced by execution: `CleanerRule(apply_to_caption=False, remove_urls=True)`
still strips `https://a.io` from a caption, because the pipeline feeds
`payload.effective_text` (`entities.py:2498`), which IS the caption for media
messages. Customer impact: an owner who turns the caption switch off still gets
captions cleaned. Classification: **missing functionality** (flag has no effect).

**Stage 4.4 resolution.** The Go pipeline takes the flag seriously. It gets a
`cleaning.CaptionHint{IsCaption}` from the payload's own `has_media` flag — the
same field Python's `effective_text` consults — and when the text is a caption
and the task's cleaner opted out (`ApplyToCaption=false`), the text cleaner is
skipped. Markup-level options (`strip_buttons`) still apply, exactly as Python's
pipeline enforces them outside `clean()`. `Apply()` keeps the pre-4.4 entry
point (no hint = treat as body), so no existing caller changes behavior.

Cross-language parity is pinned by `scripts/test_stage42_parity.py`: caption-
vs-body cases for both switch states, plus
`worker/internal/domain/cleaning/cleaning_test.go` tests covering the skip, the
opted-in default, the inactive-cleaner guard, and the compatibility entry point.

## F-10 — Feature catalog stale — **FIXED_IN_EXISTING_CODE**
`docs/feature_catalog.json` regenerated from `35ee530` in this PR (schema v3.0.0,
64 features) with the website-compatible fields wrapped and confirmed green by the
`website` CI job.

## F-11 — `block_links` evaluation ORDER differs — **FIXED (Stage 4.4)**

Python `services.py:79` checks block_links 5th; Go `filter.go:213` checked it last.
Same DROP, different reason code.

**Resolution.** The premise turned out to be inverted. `FilterRule` has **no
`block_links` field** in Python (the entity defines none, and
`sqlite_repositories.py` never sets one), so the `getattr(filter_rule,
"block_links", False)` gate at `services.py:79` **always answers False — the
gate is dead code**. Its only observable effect was the reason-code divergence
this finding tracked: a message both link-bearing and out of bounds got
`link_blocked` from Python and `too_short` from Go.
The Go engine's gate was therefore removed (kept as a documented no-op so the
field stays compatible with any older payload), and both engines now agree on
the Python order by construction.
The task-level `block_links` gate that DOES run — `media_filter.link_block_reason`
→ `content_policy.has_link` (`pipeline.py:985`) — is unaffected and is enforced
by the delivery preflight via `policy.HasLink` (the F-14 canonical predicate).
Cross-language parity pinned by 5 new `filter_engine` cases in
`scripts/test_stage42_parity.py` (74/74), including the order and the dead-flag
case, plus `filter_test.go` regression tests.

## F-12 — Go `FilterRule.BlockLinks` never populated — **RESOLVED (Stage 4.4)**
Go struct field `worker/internal/domain/rule/rule.go:101`; evaluated at
`filter.go:213`; but `rulestore.go:420-462` scans only the 9 legacy columns plus
optional `regex_block`/`regex_require`/`is_enabled` and never sets `BlockLinks`.
The coordinator re-verified in this phase that **no `block_links` column exists on
`filter_rules` in SQLite** (`database.py:54-65`) — the F-3 agent confirms the same
for PostgreSQL. Customer impact: **latent** — mitigated today because the Python
ingest gate (`pipeline.py:957`) drops the message before a job exists; the
divergence goes live when Worker activation defaults on. Classification:
**missing functionality**. Action: **Stage 4.1a (the first Stage 4.1 task)**.

**Resolution.** The projection gap was real but moot: the Python `FilterRule`
has no `block_links` field at all, so the FilterEngine gate the Go field fed
never fires on either side (see F-11). The ForwardRule-level gate — the one
production actually runs — IS projected (`rulestore.go` reads
`metadata["block_links"]`), and the delivery preflight enforces it through
`policy.HasLink`, a byte-exact port of `content_policy.has_link`. The LNK-GATE
fixtures pin both halves of that gate on the Python side.

**Store topology verified by the coordinator for the Stage 4.1 plan:**
- `filter_rules` has no `block_links` column in **any** SQLite migration. The table
  is re-defined (not ALTERed) at v1 (`database.py:54`), v11 (`:453`) and v30
  (`:923`); the v30 form adds `owner_user_id`, `regex_block`, `regex_require`,
  `is_enabled` — the three columns `rulestore.go:444-452` reads optionally. There
  are **zero** `ALTER TABLE filter_rules` statements; each migration rebuilds the
  table wholesale.
- PostgreSQL has **no `filter_rules` table at all** (`scripts/postgres/schema.sql`
  defines `forward_rules`, `delivery_jobs`, `message_map`, `dead_letter_queue`,
  `content_dedup`, `prompt_*`, `rule_stats`, `metrics_hourly`, `error_log`,
  `schema_version`, `scheduled_actions` — 11 tables, no filter table).
- The Go worker reads filters **only** from SQLite:
  `rulestore.go:394` comment "filter_rules always lives in SQLite";
  `rulestore.go:96-107` opens `filtersDB` from `ATF_DB_PATH` (SQLite) and the
  store returns an error when it is unconfigured. So `forward_rules` may live in
  Postgres while `filter_rules` stays in the Core's SQLite file.

**Correction the coordinator made to its own working assumption (verified):**
The *filter-level* `block_links` semantic exists **nowhere in the persistence
chain** — not a column, not a repository, not a proto field. The Python
`FilterRule` dataclass (`core/domain/entities.py:2312-2333`) declares **no**
`block_links` field, so `services.py:79` (`getattr(filter_rule, "block_links", False)`)
is dead code by construction. The Go `rule.FilterRule.BlockLinks` (`rule.go:101`)
is evaluated at `filter.go:213` and written by zero lines anywhere. The only real
flag is the **task-level** `ForwardRule.block_links` (metadata,
`entities.py:680-687`), gated by `media_filter.link_block_reason` at
`pipeline.py:957`.

Therefore **Stage 4.1a needs no schema change at all**: the task-level flag lives
in the `forward_rules` metadata blob, which the Go rulestore already loads and
resolves (`rulestore.go:307-324` `metaStr(...)` pattern for
`delivery_transport`, `bot_token_id`, `album_mode`, …). 4.1a projects that flag
into the Go delivery path the same way — no column, no migration, no Postgres
change. See `docs/architecture/stage-4.1-implementation-plan.md` §2.4 for the
fallback v34-column design if a future owner wants the filter-level flag real.

**Dependency order (also verified, not assumed):** 4.1b → 4.1a → 4.1c. 4.1b
first because 4.1a makes the Go gate reachable, and landing reachability first
would wire a reachable gate onto the wrong (F-14) predicate — temporarily
widening the two-sided divergence. 4.1a before 4.1c so the reason-code shift
lands in the same release the gate starts firing. Verified prerequisite: the
canonical `_RE_LINK` predicate is RE2-portable (plain literal-prefix
alternation, no Unicode property classes — unlike the F-03 emoji case).

## F-13 — Target-override strip deletes usernames in Go — **FIXED (Stage 4.2, re-verified 4.4)**
Go `transform.go:21` `urlRegex` included `@[A-Za-z0-9_]{3,32}`;
Python `remove_urls` (`services.py:159-169`) keeps mentions. Classification:
**conflicting specifications**.

**Resolution.** The dead `worker/internal/engine/transform` package that carried
that regex was superseded by Stage 4.2's `worker/internal/domain/cleaning`, and
the override path's `RemoveURLsPlain` deliberately excludes the mention
alternation (links.go: the mention arm is documented as intentionally absent —
"A mention is not a link", services.py:911). Re-verified by execution against
both engines on the `LNK-OVERRIDE-01` input: Python and Go now both return
`see @bob and` from `see @bob and https://a.io` with `remove_links: true`. The
fixture's `expected_go_output` has been corrected from the pre-4.2 divergence
(`see  and`) and the historical value is preserved in its notes.

## F-14 — Three different "contains a link" predicates — **VERIFIED**
Re-verified by execution in this phase:
- `tg://resolve?domain=x` — FilterEngine gate (`URL_REGEX` + substring probes,
  `services.py:79`): **pass**; task gate `has_link` (`content_policy.py:121`,
  `_RE_LINK` at 96-99): **DROP**.
- `email @user hi` — FilterEngine: **DROP** (URL_REGEX's mention alternation);
  `has_link`: **pass**.
Both directions of the divergence reproduced. Customer impact: messages blocked
inconsistently depending on which gate asks. Classification: **conflicting
specifications**. Action: **Stage 4.1b** — typed link classification, NOT one
broad regex.

## F-15 — `HeaderFormatter` placeholders never run on durable path — **FIXED (Stage 4.4, Go side)**

Original finding (unchanged, still accurate for the Python path):
`pipeline.py:1547-1552` applies header/footer raw; coordinator grep found zero
`HeaderFormatter` references in `pipeline.py` or `use_cases.py`. Only
`transform_text` (`services.py:307-309`) formats placeholders, and the durable
pipeline never calls `transform_text`. The gRPC `TestRule` preview
(`servicers.py:1523`) shows expanded placeholders.
Customer impact: an owner writing `{source_link}` sees it posted literally, while
the in-product preview shows it expanded. Classification: **real customer impact**.

**Stage 4.4 resolution.** `worker/internal/domain/cleaning.FormatHeader` is a
byte-identical port of `HeaderFormatter.format_text` (all 20 placeholders, the
`{x}` and `[[x]]` spellings, the private-channel `/c/<id>` link form, the
message-time `{date}`, the unknown-placeholder passthrough). The Go transform
pipeline renders the header/footer through it whenever the Worker owns the
transform stage — closing the preview/production gap for every deployment that
sets `ATF_WORKER_TRANSFORMS`. The Python durable path keeps the raw behavior
until the boundary moves, exactly as the strangler-fig model requires: one
authoritative path per job, never both.

Cross-language parity for the renderer is proven by
`scripts/test_stage42_parity.py` (6 header-template cases, including the
private-channel link and the unknown-placeholder passthrough), executed against
the real Python `HeaderFormatter` and the Go port.

## F-16 — `filter_inline_keyboard` has zero production callers — **VERIFIED**
`services.py:1065`; coordinator grep across `core`, `web`, `api`, `mobile`,
`worker` found only the definition. Customer impact: a link policy strips link
text but leaves inline URL buttons. Classification: **missing functionality**.
Action: Stage 4.4.

## F-17 — Bound disagreements between layers — **VERIFIED**
Re-verified by execution: `AdvancedSettingsEngine.get_auto_delete_seconds` with
the auto-delete add-on enabled and `auto_delete_seconds=2_000_000` returns
`2000000` (engine bound `31_536_000`, `services.py:1536`), while the registry
(`rule_settings.py`, max `604800`) would have clamped it at write time.
`forward_delay_seconds`: registry max `86400`, Go API `maxDelaySeconds=3600`
(`api/internal/handlers/handlers.go:528`).
Customer impact: low but real — a value accepted by one layer is silently clamped
by another. Classification: **conflicting specifications**.
Action: unify constants in Stage 4.4.

## F-18 — Go worker has no link policy surface — **VERIFIED**
Go has no `LinkPolicy`, no domain allow/blocklist, no rewrite map. Informational
today (payload is pre-transformed, `delivery.go:193-199`). Classification:
**architectural cleanup / migration scope**. Action: Stage 4.4.

## F-19 — Python auto-delete in-memory vs Go durable — **VERIFIED**
Python `durable_queue.py:1835` `asyncio.sleep(min(ttl, 31_536_000))`; Go's
`scheduled_actions` + `scheduleAutoDelete` is durable. Mixed activation could
double-schedule. Classification: **architectural cleanup**.
Action: retire the Python loop in Stage 4.5.

## F-20 — Thirteen settings are STORED_BUT_UNUSED beyond the findings register — **VERIFIED**
Surfaced by the Phase F persistence audit
(`docs/behavior-audit/settings-persistence-audit.md`) and confirmed against
`settings-inventory.json`: `debug_mode`, `process_raw_text`, `translate_lang`,
`news_digest`, `button_to_text`, `sender_type`, `feature_group`,
`blacklist_users`, `whitelist_users`, `auto_posting`, `power_control`,
`power_schedule`, `auto_post_scheduler` are persisted (SQLite + metadata +
backup/restore) with **zero runtime consumers**.
Classification: **architectural cleanup** (no engine reads them) with a small UI
honesty component (panels surface some of them). Action: Stage 4.5 decides per
setting whether to implement or prune. **Do not delete the settings in Stage 4.0**
— the finalization brief forbids removing settings, and their stored values must
survive any future decision.

## F-21 — Three settings are stored in two places with three readers (DUPLICATED) — **VERIFIED**
`remove_links`, `replacements` and `header` are each both a first-class
`forward_rules`-adjacent field **and** a `metadata` blob key, read by three
different code paths. Classification: **conflicting specifications** (a write
through one surface can disagree with the other). Action: Stage 4.2–4.4 picks
one home per setting during the port.

---

## Classification roll-up

| Class | Findings |
|---|---|
| Real customer impact | F-03, F-15 |
| Conflicting specifications between paths/engines | F-04, F-07, F-11, F-13, F-14, F-17, F-21 |
| Missing functionality | F-01, F-02, F-09, F-12, F-16 |
| Architectural cleanup / migration scope | F-08, F-18, F-19, F-20 |
| Intentional legacy behavior (preserve) | F-05 |
| Documentation only | F-06 |
| Fixed in this PR | F-10 |

## Emoji double-processing question — RESOLVED (Phase B.1)

Executive summary: **both paths are reachable and sequential for one message, and
the double application is harmless — it is byte-identical to a single application.**

Production order (`pipeline.py:1401-1461`): step 1 `CleanerRule.clean`
(`pipeline.py:1408`) runs, then step 3 `FilterEngine.remove_emojis`
(`pipeline.py:1461`) runs on the result when the task-level `remove_emojis` flag is
on. Both are driven by independent switches (`cleaner_config.remove_emojis` vs
`metadata["remove_emojis"]`), so both can be enabled at once and both execute.

Executed matrix (8 inputs × 4 switch combinations), production engines only:

| input | flag only | cleaner only | both | duplicate changes output? |
|---|---|---|---|---|
| plain Persian, no emoji | unchanged | unchanged | unchanged | no |
| Persian + 🌟 | `سلام  دنیا…` | `سلام دنیا…` | `سلام دنیا…` | **no** |
| `👨‍👩‍👧` ZWJ family | `\u200d\u200d` | `\u200d\u200d` | `\u200d\u200d` | no |
| 🇮🇷 flag | removed | removed | removed | no |
| 5️⃣ keycap | `5⃣` | `5⃣` | `5⃣` | no |
| 𒀀 (non-emoji SMP) | destroyed | destroyed | destroyed | no |
| ❤️ + ← | heart gone, arrow kept | same | same | no |
| all mixed | same | same | same | no |

The emoji removal itself is **idempotent** — running the same regex twice on the
same string cannot change it, so `both_enabled == cleaner_only` byte-for-byte in
all 8 cases. The one observable difference is **whitespace**: `CleanerRule.clean`
collapses `[ \t]{2,}` and `\n{3,}` (`entities.py:2402-2404`) while the flag path
only does `.strip()` — so on mixed text the flag-only variant leaves a double
space (`سلام  دنیا`) where the cleaner variant does not. That is a cosmetic
divergence between configurations, not a double-processing defect.

Verdict: **harmless, but a semantic duplication** — two switches named
`remove_emojis` on two different stores doing the same thing. The behavior is
configuration-dependent in effect only through the whitespace tidy, not through
the emoji pass. Preserve both paths' outputs exactly in Stage 4.2; the fix for
F-03 (the pattern) is what actually matters, and any new pattern must be applied
by exactly one path or kept idempotent as it is today.
