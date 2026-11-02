# Coordinator — confirmed findings register

Baseline: master `35ee530`. Branch `audit/complete-business-logic-inventory`.
This register holds only findings the **coordinator re-verified against source or
by execution**. Agent self-reports are not carried here until re-verified.
Each entry gets a stable finding ID. Fix proposals go to a separate backlog PR —
NOT into the inventory PR.

---

## VERIFIED FINDINGS

### F-01 — `dedupe_policy` is STORED_BUT_UNUSED  (severity: MEDIUM)
- Field: `core/domain/entities.py:244` (`STRICT | RELAXED | DISABLED`, default STRICT)
- Written by SQLite repo (`sqlite_repositories.py:388,654`), Postgres repo
  (`postgres_repositories.py:378,723`), gRPC proto (`dedupe_policy` field 30 on
  ForwardRule), backup (`backup_service.py:201,601`).
- **Runtime consumers: ZERO.** Coordinator grep across `core/` found only
  definition + persistence + proto + backup. No read in `pipeline.py`,
  `durable_queue.py`, `dispatcher.py`, `services.py` or `sync_engine.py`.
- Impact: an owner switching STRICT→DISABLED changes nothing.

### F-02 — Album aggregation TTL is hard-coded  (severity: LOW)
- `core/infrastructure/telegram/dispatcher.py:96` constructs
  `AlbumAggregator(flush_callback=..., ttl_seconds=2.0, max_active_albums=500)`.
- `ForwardRule.album_aggregation_window_seconds` (`core/domain/entities.py:410`,
  default 0.8) is never read by the dispatcher.
- Impact: per-task album window setting has no effect.

### F-03 — Emoji regex destroys non-emoji SMP and leaves ZWJ/keycap fragments  (severity: MEDIUM)
Three byte-identical copies of one pattern — a design smell as well as a bug:
- `EMOJI_REGEX` — `core/domain/services.py:31-34`
- `_CLEANER_EMOJI_RE` — `core/domain/entities.py:103-106`
- `emojiRegex` — `worker/internal/engine/transform/transform.go:33`

Pattern: `[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]|[\ufe00-\ufe0f]`

**Coordinator execution results** (Python `re`, 2026-11-01):

| Input | Output | Verdict |
|---|---|---|
| `👨‍👩‍👧` (ZWJ family) | `\u200d\u200d` | leaves 2 dangling ZWJ |
| `5️⃣` (keycap) | `5⃣` | leaves dangling U+20E3 |
| `❤️` | `` | removes the heart (U+2764 ∈ misc symbols) |
| `𒀀abc` (Cuneiform) | `abc` | destroys non-emoji SMP script |
| `⌘` | `` | removes misc-technical symbol |
| `←` (arrow, U+2190) | `←` | NOT removed (2190-21FF not matched) |
| `á` (combining mark) | `á` | NOT removed |
| `‍` (bare ZWJ) | `‍` | NOT removed |
| `🇮🇷` (flag) | `` | removed (both regional indicators are SMP) |
| `👍🏽` (skin tone) | `` | removed (both SMP) |
| `سلام 🌟 دنیا` | `سلام  دنیا` | Persian text intact; double space left |

**Python and Go produce identical output on every fixture** — the pattern is
wrong, not the ports. Fix must be one Unicode-property-based pattern shared by
both languages (separate fix PR; needs grapheme/emoji-property regex, which
Go RE2 handles via `\p{Emoji}`-class properties only partially — design care
needed).

### F-04 — `remove_links`/`link_replacement` precedence differs per path  (severity: MEDIUM)
- Durable path `core/application/pipeline.py:1438` is an `if ... elif`:
  `remove_links=True` wins and `link_replacement` is silently ignored
  (`elif` at `pipeline.py:1455`).
- Legacy `FilterEngine.transform_text` (`core/domain/services.py:268-271`) gives
  `link_replacement` precedence.
- Impact: same task configuration produces different output on the durable path
  vs the edit-sync/gRPC-preview path.

### F-05 — `forward_every_n` runs AFTER all filters  (severity: INFO — corrects the brief)
- `pipeline.py:1036-1084`, i.e. after `FilterEngine.evaluate` (918), media/link
  gates (955) and the content-policy chain (974-1016). Filtered messages consume
  no slot. Albums consume one slot (gate sits after aggregation).
- The brief's §12 outline placed it before transformation only — accurate, but
  any doc showing it before the filter stage is wrong.

### F-06 — `materialize()` (bot defaults) precedes EVERY gate  (severity: INFO)
- `pipeline.py:787`. The paused check, `since_ts`, filters and content policies
  all read the bot-defaults-merged view. A bot default cannot save a message the
  task's own policy rejects (additive-only), but it CAN change what a task that
  set nothing itself does.

### F-07 — Cleaner mention/hashtag/phone regexes diverge across the three engines  (severity: MEDIUM)
**Mention** — coordinator-verified by execution:

| Regex | Source | `@abc` | `@abcd` |
|---|---|---|---|
| `@[A-Za-z0-9_]{4,64}` | cleaner `entities.py:101` | kept | removed |
| `@[A-Za-z0-9_]{3,32}` | legacy `services.py:27,187` + Go `transform.go:28` | removed | removed |

**Hashtag** — `#hello-world`:

| Regex | Source | Result |
|---|---|---|
| `#\w+` | cleaner `entities.py:102` | `#hello` (leaves `-world`) |
| `#\S+` | legacy `services.py` | `#hello-world` |
| `#[^\s#@]+` | Go `transform.go:24` | `#hello-world` |

**Phone** — cleaner uses a guarded candidate+callback
(`entities.py:111-131`, 8-15 digits, protects bare product ids); Go uses
`\+?\d[\d\s\-()]{7,18}\d` which matches inside longer digit runs and eats
product ids and IP-like strings.

### F-08 — The entire Go `transform` package is UNREFERENCED at delivery time  (severity: HIGH for migration)
- `worker/internal/engine/delivery/delivery.go:87` holds a `*transform.Engine`
  and `delivery.go:120` constructs it — but coordinator grep confirms
  `ApplyTargetOverride`, `RemoveEmojis`, `Hashtags`, `Usernames`, `Tidy`,
  `Clamp`, `HasLink` and `FormatText` have **no production call sites**.
- `TargetOverride` appears in no `.proto` and no store.
- So today the Go worker applies NO task-level cleaner/replace/emoji/header
  transforms; all of that is baked into the job payload by the Python pipeline.
  This is *intended* for Stage 3 (comment at `delivery.go:193-199`) but the
  package's doc claims otherwise, and the mission brief's audit target #4 is
  exactly this confusion. Statuses must record this as UNREFERENCED_HELPER, not
  PARTIALLY_IMPLEMENTED.

### F-09 — `apply_to_caption=False` cannot disable caption cleaning  (severity: MEDIUM)
- `CleanerRule.apply_to_caption` (`entities.py:2366`) defaults True and is
  respected by `build_cleaner()`, but the pipeline feds
  `payload.effective_text` (`entities.py:2498-2502`), which already returns the
  caption for media messages. So the flag is STORED_BUT_UNUSED by the engine.

### F-11 — `block_links` evaluation ORDER differs Python vs Go  (severity: MEDIUM, not HIGH)
- **Python** `core/domain/services.py:79-81`: `block_links` is checked **5th**,
  immediately after the media allow/deny gates and BEFORE min/max length.
- **Go** `worker/internal/engine/filter/filter.go:213-219`: the same gate is the
  **LAST** check, after length bounds, blacklist, whitelist and both regex lists.
- Coordinator-verified by execution: a message that is both link-bearing and
  over `max_message_length` gets `link_blocked` from Python and `too_long` from
  Go. **Same DROP decision, different reason code** — so it is a
  metrics/logging/audit divergence, not a message-loss divergence.
- Migration note: when the Go filter becomes authoritative, drop-reason
  dashboards and any alerting on `link_blocked` shift. Fix is a one-line reorder
  in the Go engine (separate PR).

### F-12 — Go `BlockLinks` exists in the engine but is NEVER populated  (severity: HIGH)
This is the sharper half of Agent A's P-ORD that the agent did not call out.
- The Go rule struct HAS the field: `worker/internal/domain/rule/rule.go:101`.
- The Go engine DOES evaluate it: `worker/internal/engine/filter/filter.go:213`.
- But `worker/internal/rulestore/rulestore.go:420-462` (the filter projection)
  **never sets `f.BlockLinks`**: it scans the 9 legacy columns
  (whitelist/blacklist/regex_patterns/allowed/blocked/drop_service/min/max) plus
  optional `regex_block`, `regex_require`, `is_enabled` — and stops. There is no
  `block_links` column on `filter_rules` at all (schema at
  `core/infrastructure/persistence/database.py:54-65`).
- **Consequence: on the Go delivery path, an attached FilterRule with
  `block_links=true` forwards link-bearing messages.** The Python pipeline would
  drop them. That is a real behavioral divergence TODAY, not a logging one.
- Root cause: `block_links` on the Python side is a *task-level* setting
  (`ForwardRule.block_links`, `entities.py:680`, metadata) that also exists on
  the FilterRule dataclass; the pipeline gates it at `pipeline.py:957` via
  `link_block_reason(rule, ...)` — the **rule-level** gate, which the worker does
  not re-run either. So BOTH link gates are missing on the Go delivery path.
- Mitigating context: per `delivery.go:193-199`, ingest-side decisions are
  "baked into the job payload" when the Python pipeline enqueues — so in the
  current activation state the Python gate already dropped the message before a
  job exists. The divergence is latent until Worker activation becomes default.

### F-13 — Agent A P-LNK: target-override link removal diverges  (severity: MEDIUM)
- Python `FilterEngine.remove_urls` (`services.py:159-169`) unwraps markdown
  anchors `[text](url)` keeping the anchor text, and keeps `@usernames`.
- Go `transform.ApplyTargetOverride` (`transform.go:62-66`) uses
  `urlRegex.ReplaceAllString` whose pattern (`transform.go:21`) INCLUDES
  `@[A-Za-z0-9_]{3,32}` — so the Go strip deletes usernames too.
- Same divergence family as F-07 (the Python `urlRegex` also includes the
  mention alternation, but `remove_urls` does not use `urlRegex` — it has its own
  sub-patterns). Cross-check against F-07/F-04 before writing the fix.

### F-14 — Agent A P-LINKGATE: three different "contains a link" predicates  (severity: MEDIUM)
Three non-equivalent link predicates are in production:
1. **FilterEngine gate** — `URL_REGEX.search OR "http://" in OR "https://" in OR
   "t.me/" in` — `core/domain/services.py:79-81`; the same four-clause form in Go
   (`worker/internal/engine/filter/filter.go:213-219`).
2. **`has_link()`** — `core/domain/content_policy.py:121` backed by `_RE_LINK`
   (`content_policy.py:96-99`): `(?:https?://|ftp://|www\.|t\.me/|telegram\.me/|
   telegram\.dog/|tg://)`. Used by `link_block_reason` (`media_filter.py:94`) at
   `pipeline.py:957`, and by `ContentPolicy.evaluate` itself
   (`content_policy.py:839`).
3. **`LinkManagementEngine`** policy predicates — `services.py:836+`.

**Coordinator execution proof** (both predicates run on the same strings):

| text | FilterEngine `block_links` | task `link_block_reason` |
|---|---|---|
| `see tg://resolve?domain=x` | **pass** | **DROP** |
| `ftp://files.example/x` | **pass** | **DROP** |
| `visit t.me/foo` | DROP | DROP |
| `see https://a.io` | DROP | DROP |
| `no link here at all` | pass | pass |

So a task with `block_links=true` answers "does this message carry a link"
differently depending on whether the attached FilterRule or the rule-level gate
asks the question. `"email @user hi"` also DROPs under gate 1 (URL_REGEX's
`@[A-Za-z0-9_]{3,32}` alternation) but passes gate 2 — a mention is not a link
to `has_link`. Two-sided divergence, verified.


### F-10 — Feature catalog and switch matrix are stale  (severity: blocking for this audit)
- `docs/feature_catalog.json` → `generated_from: master@d05f059`; **440 commits
  behind** master `35ee530`; 19 features, none of them the filter/cleaner/
  replace/content-policy surface.
- `docs/task-feature-switch-matrix.md` → evidence from `sha-9aeb96d` /
  `sha-649c969`; 14 of 28 switches still `PENDING`.


### F-15 — `HeaderFormatter` placeholders never run on the durable path  (severity: HIGH)
Agent D HDR-P1, coordinator-verified.
- `HeaderFormatter.format_text` (`core/domain/services.py:320-389`) interpolates
  21 placeholders — `rule_name`, `category`, `date`, `source_link`, `sender_name`,
  `chat_title`, `source_chat`, plus the `ORIGIN_*` / `SOURCE_NAME` /
  `SENDER_CHAT` / `FORWARD_FROM_CHAT` / `FROM_USER` shortcodes — and accepts both
  `{key}` and `[[key]]` spellings.
- **The only production caller is `FilterEngine.transform_text`
  (`services.py:307-309`)**, which the durable pipeline does not call (see F-16).
  Coordinator grep across `core/application/pipeline.py` and
  `core/application/use_cases.py` found **zero** `HeaderFormatter` references.
- The durable path applies header/footer raw:
  `pipeline.py:1547-1552` and `use_cases.py:998-999` call
  `apply_header_footer(text, header=rule.header, footer=rule.footer)` with the
  template unformatted.
- **Impact: an owner who writes `{source_link}` or `[[chat_title]]` in a header
  gets the literal text `{source_link}` posted to the target on the production
  path**, while the gRPC `TestRule` preview (`servicers.py:1523`, which routes
  through `TransformationService`/`transform_text`) shows it correctly expanded.
  The preview lies about what production delivers.
- Only tests reference `HeaderFormatter` directly (`test_filter_engine.py:114`).

### F-16 — Agent D LNK: `filter_inline_keyboard` has zero production callers  (severity: MEDIUM)
- `LinkManagementEngine.filter_inline_keyboard` (`services.py:1065`) filters URL
  buttons in an `InlineKeyboardMarkup` under the link policy.
- Coordinator grep across `core`, `web`, `api`, `mobile`, `worker` (py/ts/tsx/
  dart/go) found only the definition. **Zero callers.**
- Impact: a task with `link_policy=REMOVE_ALL_URLS` strips link *text* but leaves
  inline URL **buttons** untouched. The policy promises "remove all URLs" and
  delivers a message whose keyboard still opens them.

### F-17 — Agent D ADV-P1/P4: bound disagreements between layers  (severity: MEDIUM)
| Setting | Registry (`rule_settings.py`) | Engine (`services.py`) | Go API |
|---|---|---|---|
| `auto_delete_seconds` | max **604 800** (7 days, `:155`) | **31_536_000** (1 year, `:1536`) | — |
| `forward_delay_seconds` | max **86 400** (`:153`) | `MAX_FORWARD_DELAY_SECONDS` | `maxDelaySeconds = 3600` (`api/internal/handlers/handlers.go:528`) |

The registry is what the validated settings API and dashboard enforce; the
engine bound is what actually runs. A task that saves `auto_delete_seconds =
2_000_000` is accepted by the API, clamped to 604 800 in the registry view and
to 31_536_000 at execution. Python's own in-memory auto-delete
(`durable_queue.py:1835`) uses the engine bound.
Also `max_time_edit` (registry max 86400) vs the edit-window semantics at
`services.py:1513` — separate code path, same class of disagreement.

### F-18 — Agent D: Go worker has no link policy surface at all  (severity: INFO)
Go has no `LinkPolicy`, no domain allowlist/blocklist, no rewrite map, and no
inline-keyboard filter. Today this is informational (the payload arrives
pre-transformed per `delivery.go:193-199`), but it defines the Stage 4.4 scope:
the whole `LinkManagementEngine` must be ported, not just the strip path.

### F-19 — Python auto-delete is in-memory, Go's is durable  (severity: MEDIUM)
- Python path: `asyncio.sleep(min(ttl, 31_536_000))` at
  `core/infrastructure/telegram/durable_queue.py:1835` — lost on restart.
- Go: `worker/internal/engine/scheduler` + `delivery.go` `scheduleAutoDelete`,
  persisted (`scheduled_actions`) and claimed atomically.
- If both paths are active (mixed activation), a message can be scheduled twice.
  Stage 3 added the durable scheduler; the Python loop should be retired when
  the worker owns delivery — track in the migration plan, not as a fix PR.

---

## DOWNGRADED / NOT CONFIRMED

### Agent C P-2 (keyword trigger reads pre-transform text) — CONFIRMED, severity LOW (not MEDIUM)
`core/application/pipeline.py:1304`:
```python
reply_text = AdvancedSettingsEngine.classify_keyword_trigger(
    payload.effective_text or ctx.transformed_text or "", rule,
)
```
`ctx.transformed_text` is only consulted as a **fallback** when
`payload.effective_text` is empty. Because `or` short-circuits, the *source*
text wins whenever it exists. The comment above the call (`pipeline.py:1301`)
even frames this as a deliberate choice ("A matched whitelist keyword schedules
the configured reply"). So: a keyword match can be computed from text the target
never receives (the header/footer/watermark are absent, and transforms have
already run), but the reply is a secondary best-effort side-effect that never
drops the forward. Real, low severity, by design.

### Agent C P-4/P-6 → coordinator F-02 — CONFIRMED

## OPEN ITEMS (coordinator must verify before the final report)

- [x] Read `core/application/pipeline.py:1290-1320` to settle where
      `classify_keyword_trigger` gets its text (Agent C P-2). — settled above
- [x] Read `core/application/use_cases.py:940-985` to confirm the reply path
      really runs cleaner + replace (Agent B CLN-USECASE / RPL-USECASE).
      CONFIRMED: cleaner → link_replacement/remove_links (same `if`/`elif`
      shape as the durable path) → remove_emojis → replace_text → AI.
- [ ] Agent B P-6 "emoji stripped twice": `pipeline.py:1413` (cleaner) and
      `pipeline.py:1460` (flag) — confirm both are reachable on one message
      (they are two different switches, `cleaner_config` vs `remove_emojis`;
      a task setting both strips twice — needs a fixture, not a fix).
- [x] Settle `FilterEngine.transform_text` production reach: production callers
      are exactly `sync_engine.py:261` (edit sync) and `servicers.py:1523`
      (gRPC `TestRule` preview). `services.py:412` is `TransformationService`
      (used by the gRPC preview surface). NOT reached by the durable pipeline.

