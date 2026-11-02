# Agent S2 — FilterRule length/codepoint fix + RuleStore block_links projection (F-12) + integration

Repo /root/AutoTelegramForward, branch `feat/stage-4.1-canonical-content-policy` (stacked on audit branch a817c59). Implementation stage: Stage 4.1. **No merging/deploying. Never force-push.**

## Context (verified during F-12 revalidation — DO NOT re-derive)
F-12 verdict is **(A): existing intended feature missing from the Go projection.** Proof:
- `block_links` is a **task-level** setting everywhere: bot UI `pro_bot_ui.py:6804` (`rfl_lnk:` callback on `_owned_rule` = IForwardRuleRepository), Go API `handlers.go:491` (settings allowlist), Flutter `rule_settings_sheet.dart:94`.
- Python enforces it at `core/application/pipeline.py:957` via `link_block_reason(rule, ...)` reading `ForwardRule.block_links` (metadata, `entities.py:678`).
- The Python `FilterRule` dataclass (`entities.py:2312-2333`) has NO `block_links` field; `services.py:79` `getattr(filter_rule,"block_links",False)` is dead.
- SQLite `filter_rules` has no `block_links` column in ANY migration (v1/v11/v30 rebuilds only). PostgreSQL has NO `filter_rules` table. Go rulestore reads filters only from SQLite (`rulestore.go:94-106`).
- `worker/internal/rulestore/rulestore.go:307-324` ALREADY loads the task metadata blob and resolves `delivery_transport`, `bot_token_id`, `album_mode` etc. via `metaStr`/`metaInt64` (:352-375).
So the fix is: project the task-level metadata flag. **NO database migration, NO new column, NO new setting.**

Also confirmed by coordinator execution: Go `filter.go:138,141` uses `len(text)` (UTF-8 bytes) but Python `services.py:83,86` uses `len(text)` (codepoints). Persian `سلام دنیا` = 9 codepoints but 17 bytes. This is a real divergence to fix.

## Your scope (files only you modify/create)
1. `worker/internal/engine/filter/filter.go` — fix the two length checks to codepoints
2. `worker/internal/engine/filter/filter_test.go` — add Persian/Arabic/emoji length tests (extend the existing file)
3. `worker/internal/domain/rule/rule.go` — add `BlockLinks` to `ForwardRule` (task-level) and fix the `FilterRule.BlockLinks` comment to say it mirrors the task flag
4. `worker/internal/rulestore/rulestore.go` — project the task's `block_links` metadata flag; add a `metaBool` helper next to `metaStr`/`metaInt64`
5. `worker/internal/engine/delivery/delivery.go` — apply the task-level BlockLinks gate at the right point in the preflight
6. `worker/internal/rulestore/rulestore_test.go` — add a test that a row with metadata `{"block_links": true}` yields BlockLinks=true
7. `worker/internal/engine/delivery/delivery_test.go` — add a test that a link-bearing message on a task with BlockLinks is dropped with reason `link_blocked`

**Do not touch:** `worker/internal/domain/policy/*` (Agent S1 owns those), anything under `core/`, any database schema file, any `.proto`.

## Hard requirements

### filter.go length fix
Replace `len(text)` with `utf8.RuneCountInString(text)` at the min/max length checks ONLY. Do not change evaluation order, reason strings, or any other check. Match Python `len()` semantics exactly (codepoints).

### rulestore projection (F-12, verdict A)
- Add `metaBool(meta map[string]any, key string, def bool) bool` next to `metaStr` (`rulestore.go:352`) mirroring Python's `bool(self.metadata.get(key, def))`.
- In `getRule` where `extra.Metadata != nil` (around `:331-338`), add: `extra.BlockLinks = metaBool(extra.Metadata, "block_links", false)`.
- Add `BlockLinks bool` to the `ForwardRule` struct in `worker/internal/domain/rule/rule.go` with a comment: "mirrors the task-level metadata flag the Python pipeline gates at pipeline.py:957 (media_filter.link_block_reason); the persisted filter row has no such column."
- In `FilterRule.BlockLinks` comment at `rule.go:101`: correct it to say it mirrors the task's flag projected at load, NOT a filter-row column.

### delivery.go integration
The preflight at `delivery.go:212` re-evaluates the attached FilterRule on the baked payload text. Add the **task-level** BlockLinks gate using the canonical link predicate from Agent S1's `worker/internal/domain/policy` package (`policy.HasLink`). Place it immediately before or after the attached-filter step so it runs at the same "permanent decision" point. Reason string: `link_blocked`. A drop must call `e.store.MarkFailed` like the filter drop does (never leave an unresolved fence claim).
- Import `atf/worker/internal/domain/policy`. If Agent S1 has not finished the package when you get there, define the call against the documented signature `policy.HasLink(text string) bool` and note it.

### Tests
- `filter_test.go`: Persian, Arabic, emoji, and mixed length boundaries — e.g. max_message_length=9 must ALLOW "سلام دنیا" (9 codepoints) where the old byte-length code dropped it (17 bytes). Assert reason `too_short`/`too_long` still fire for english.
- `rulestore_test.go`: seed a SQLite row with metadata `{"block_links": true}` → `GetByID` returns `BlockLinks=true`; metadata `{}` or missing → `false`.
- `delivery_test.go`: a job whose rule has BlockLinks=true and payload text contains `https://example.com/x` → job is MarkFailed with reason containing `link_blocked`; a clean-text job proceeds.
- Reuse existing test helpers in those files — do not invent a new harness.
- `go test ./...` and `go test -race ./...` must pass in `worker/`.

## Report
Files changed with line refs, the exact diff for the 2-line length fix, test commands and pass counts, confirmation that NO schema/proto file was modified, remaining uncertainty. Do not claim parity without a pasted test result.
