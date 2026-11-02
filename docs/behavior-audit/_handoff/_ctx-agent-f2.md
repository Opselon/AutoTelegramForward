# Agent F2 — Go behavioral fixture runner

Repo /root/AutoTelegramForward, branch `audit/complete-business-logic-inventory` (PR #289, head c74498d, CLEAN, 16/16 green). Discovery + test-infra only. **Do not merge, deploy, or touch production code.**

## Your scope (files only you create)
1. `worker/internal/audit/fixtures_test.go` — loads and validates the shared fixture file
2. `worker/internal/engine/filter/filter_fixture_test.go` — runs filter fixtures against the REAL Go filter engine
3. `worker/internal/engine/transform/transform_fixture_test.go` — runs the transform/emoji fixtures against the REAL Go engine
4. `worker/internal/audit/audit_test.go` — schema validation

**No other file.** Never edit `fixtures.json`, production Go code, or another agent's file.

## Shared contract
Read `docs/behavior-audit/_handoff/_fixture-contract.md` FIRST. Routing classes: `python`, `both`, `meta`, `schema`, `unclassified`.

## Task 1 — Go fixture loader
Load `docs/behavior-audit/fixtures.json` by walking up from the test file (the worker module root is `/root/AutoTelegramForward/worker`; the repo root is one level up). Decode into a struct with the fields: `id`, `engine`, `group`, `description`, `executable`, plus a generic `input map[string]any` and `expected_go map[string]any` (or the `expected_go_output` string). Unknown `executable` tokens and duplicate ids must FAIL validation.

## Task 2 — run Go-executable fixtures against real engines
- `worker/internal/engine/filter` `Engine.Evaluate(text, mediaType, isService, f *rule.FilterRule)` for `FilterEngine.evaluate` fixtures. Build `rule.FilterRule` from the fixture's `input.rule` map.
- `worker/internal/engine/transform` for the `both`-class fixtures: `RemoveEmojis`, `Hashtags`, `Usernames`, `Tidy`, `HasLink`, `ApplyTargetOverride`, `Clamp`. The Go side of `UNI-EMOJI-*` must run `RemoveEmojis`.
- Assert `expected_go.action`/`reason` and `expected_go_output`. Failure messages must print fixture id + engine + expected vs actual.

## Task 3 — unsupported must be visible, never fake parity
For fixtures whose `executable` is `python` (no Go engine exists — ContentPolicy, CleanerRule, apply_replacements, AdvancedSettingsEngine, LinkManagementEngine), the harness must report them as `UNSUPPORTED_IN_GO` in a summary test — never silently skipped and never reported as parity. Assert the count matches the classification so a fixture reclassification is caught. For `both` fixtures with differing Python/Go expectations, assert the Go expectation and tag the test with the finding id in its name (e.g. `TestFixture/CLN-PHONE-01//F-07_divergence`).

## Task 4 — run and report
`cd /root/AutoTelegramForward/worker && go test ./internal/audit/... ./internal/engine/filter/... ./internal/engine/transform/... -v` then `go test ./...` and `go vet ./...`. Report: files changed, exact commands, pass/fail counts, any fixture whose Go expectation was wrong, remaining uncertainty. **Do not** run `go test -race` for longer than 120s and never edit production code to make a test pass.
