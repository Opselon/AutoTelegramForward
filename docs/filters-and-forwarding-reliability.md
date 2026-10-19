# Feature expansion & forwarding reliability

Scope: per-task Cleaner, Video/Audio/Sticker/Text filters, `/profile` recovery
controls, invalid source/target handling, and Telegram account time limits.
Every claim below names the code path and the test that exercises it; a screen
rendering or a row existing is not treated as evidence.

## What already existed (audited, not rebuilt)

| Area | Existing implementation | Gap found |
|------|------------------------|-----------|
| Cleaner | `ForwardRule.cleaner_enabled` / `cleaner_config` / option flags, `build_cleaner()`, bot menu `rcl:*` (8 options + master switch), web dashboard *Cleaner Protocol* tab, Flutter model fields, `cleaner_enabled`/`cleaner_config` in the Go API parity list | only the durable pipeline applied it; the reply / private-view path forwarded raw text; cleaner settings did not travel through `to_draft()`/`apply_draft()`, so a duplicate/re-save silently switched them off |
| Filters | `block_voice`, `block_stickers` enforced in the pipeline; six flags written by the bot | `block_video`, `block_audio`, `block_text`, `block_links`, `allowed_media_types` were written by the UI but enforced on only some paths; Go `parityKeys` dropped them, so dashboard/app saves never reached the engine; no toggles in the dashboard/app |
| `/profile` | `_render_profile_menu`, `pr_restart`, `pr_reset_cfg`, `pr_reset_cfg_yes`, 10-language copy | "Reset config" reset headers/footers and wiped the metadata blob while leaving every column-level cleaner/filter flag set; the confirmation promises cleaner + filters + blocked words |
| Invalid chats | `errors.tg_error`, hard-coded permanent-chat string list in the queue, `is_active=False` on failure | one dead chat disabled the whole task with no stored reason and no notice; every `CRITICAL` error was treated as permanent (network blips disabled tasks) |
| Account limits | `ClientPool` in-memory `session → until` dict, `FloodWait` branch in the queue | process-local (a restart or a second replica retried into a longer wait); *any* "recoverable" error suspended the account, using the jittered backoff as the duration; messages arriving during a suspension were dropped |

## What was implemented

### 1. Per-task Cleaner

* `core/domain/profile_recovery.py` — one definition of what a settings reset
  clears, used by the bot callback.
* `MessageForwardingUseCase` (reply / private-view path) now runs the same
  `rule.build_cleaner()` before the link/text transforms, and honours
  `strip_buttons`. Previously this path delivered uncleaned text.
* `to_draft()` / `apply_draft()` now carry `cleaner_enabled`, `cleaner_config`
  and every option flag, so duplicate/recorder flows cannot disable a cleaner.

### 2. Video / Audio / Sticker / Text filters

* `core/domain/media_filter.py` — `MEDIA_TYPES`, `normalise_media_type`,
  `media_block_reason` (allow-list first, then the six block flags),
  `link_block_reason`. One decision, used by the pipeline, the use case and the
  rule-preview engine.
* `core/domain/rule_settings.py` — `allowed_media_types` registered as a real
  list setting (it already existed in the API/backup contract but was not read
  by Core).
* Go `parityKeys` + typed metadata list: `block_video`, `block_audio`,
  `block_text`, `block_links` now reach the rule from the dashboard/app, and a
  JSON `false`/`0` is coerced to `false` rather than truthy.
* Dashboard (`App.tsx`, `TaskOverviewModal.tsx`) and Flutter
  (`models.dart`, `rule_editor_screen.dart`, 10 l10n files) expose the toggles.

### 3. Profile recovery controls

* `/profile` → *Restart setup* clears only the `UiState` conversation; rules,
  sessions and settings are untouched and the fact is logged
  (`[PROFILE:RESTART_SETUP]`).
* `/profile` → *Reset config* asks for explicit confirmation, then calls
  `reset_rule_settings()`: headers, footers, replacements, cleaner (master
  switch, every option, custom words, config) and all six content filters.
  It never changes `id`, `owner_user_id`, `session_id`, `source_chat_id`,
  `target_chat_id`, `is_active`, `filter_rule_id` or `bot_token_id`, and never
  deletes a session or a rule.

### 4. Invalid source/target handling

* `core/infrastructure/telegram/failure_policy.py` —
  `classify_failure(exc)` → `account` / `permanent` / `transient`, reading the
  normalised model in `errors.py` instead of a second string table.
* Permanent chat errors (deleted channel, bot kicked, banned user) pause
  **only the affected rule**: `is_paused` plus an `auto_pause`
  `{code, reason, chat_id, scope, at}` in metadata, queued work failed with
  `Task paused: <reason>` (`[PIPELINE:DROP]`), and the owner is notified once
  through `notify_task_event` with the reason and a *Check & resume* button.
  Resuming clears the pause (`clear_auto_pause()`).
* Transient errors keep the ordinary retry/backoff path and never pause.

### 5. Telegram account time limits

* `core/infrastructure/persistence/account_limits.py` + migration **v26**
  (`account_limits` table, `session_id` primary key) — durable, shared state.
* `ClientPool` takes a store, hydrates at startup (`hydrate_limits()`), checks
  memory first and the row second, and `set_time_limit()` only ever extends a
  deadline (a shorter later wait cannot shorten a suspension).
* `rate_limit_seconds(exc)` is deliberately strict: `FLOOD_WAIT*` /
  `SLOWMODE_WAIT*` ids or Flood/Slowmode classes **with** a Telegram-supplied
  duration. A connection error is not a rate limit.
* The queue suspends for Telegram's own duration, deduplicates the owner notice
  per suspension (`account_restricted`), and the ingest pipeline **queues**
  messages that arrive during a suspension instead of dropping them, so the
  task resumes with its backlog when the restriction expires.

## Test evidence

| File | Tests | Proves |
|------|-------|--------|
| `core/tests/test_media_type_filters.py` | 21 | one filter decision across pipeline, use case and preview, per `MediaType`, against real SQLite |
| `core/tests/test_invalid_chat_autopause.py` | 9 | real pyrogram error classes pause only the owning task, store the reason, notify once; transients never pause |
| `core/tests/test_account_time_limits.py` | 9 | strict rate-limit detection, durable store, restart/replica visibility, monotonic deadlines, Telegram's duration (not the jittered backoff), one notice per suspension, queued-not-dropped ingest |
| `core/tests/test_cleaner_and_profile_parity.py` | 7 | cleaner on the reply path, button stripping, draft round-trip, legacy draft keeps the master switch, reset preserves identity/routing and is idempotent |

Targeted host run: **109 passed** (filters + reliability + existing queue,
pipeline and cleaner suites). Full suite, Go, Rust, frontend and Docker checks
run in GitHub CI.

## Known limits (not claimed as working)

* `account_limits` lives in the SQLite database the core process shares
  (`ATF_DB_PATH`). In a Postgres-queue deployment the coordination point is
  still that shared SQLite file; a fully Postgres-native limit table is a
  follow-up, not part of this change.
* An operator-initiated resume (`ClientPool.resume_account`) exists as a port;
  no HTTP route exposes it yet — recovery after expiry is automatic.
* The older findings recorded in `docs/feature-coverage-matrix.md`
  (`paid_addons` key mismatch, auto-delete restart, F24/F28) are unchanged by
  this batch.
