# Agent F1 — Fixture schema validator + Python harness

Repo /root/AutoTelegramForward, branch `audit/complete-business-logic-inventory` (PR #289, head c74498d, CLEAN, 16/16 green). Discovery + test-infra only. **Do not merge, deploy, or touch production code.**

## Your scope (files only you create)
1. `core/tests/audit/__init__.py`
2. `core/tests/audit/test_fixture_schema.py`
3. `core/tests/audit/test_fixtures_python.py`
4. `core/tests/audit/conftest.py` if needed

**No other file.** Never edit `fixtures.json`, the Go worker, or another agent's file.

## Shared contract
Read `docs/behavior-audit/_handoff/_fixture-contract.md` FIRST and obey it exactly. Routing classes are `python`, `both`, `meta`, `schema`, `unclassified`.

## Task 1 — schema validator (`test_fixture_schema.py`)
Implements the 7 integrity gates from the contract. Walk up from the test file to find the repo root and load `docs/behavior-audit/fixtures.json`. Fail loudly on: unknown `executable` token, duplicate id, missing required field, `input` not a dict, no expected-result key, stray `-*` placeholder keys. Validate that `both` fixtures carry both language expectations. Report counts of each class in a test so the coverage denominator is visible.

## Task 2 — Python harness (`test_fixtures_python.py`)
Runs every fixture whose `executable` is `python`, `both`, or `meta` against REAL production engines. Import from:
- `core.domain.services` → `FilterEngine`, `EMOJI_REGEX`, `FilterEngine.remove_urls/remove_links/remove_usernames/remove_hashtags/remove_phones/remove_emojis/replace_links/apply_header_footer/transform_text`
- `core.domain.entities` → `ForwardRule`, `FilterRule`, `CleanerRule`, `MessagePayload`
- `core/domain.content_policy` → `ContentPolicy`, `has_link`
- `core.domain.media_filter` → `media_block_reason`, `link_block_reason`
- `core.domain.text_replace` → `apply_replacements`, `entry_from_value`
- `core.domain.services` → `AdvancedSettingsEngine` (classify_keyword_trigger, get_auto_delete_seconds), `HeaderFormatter`

Fixture `input` shape is engine-specific — build small adapters keyed on `f["engine"]`. Construction helpers:
- `ForwardRule(id=.., session_id="s", source_chat_id="-1001", target_chat_id="-1002", routing_type="CHANNEL_TO_CHANNEL", forward_mode="COPY_MESSAGE", is_active=True)` then set metadata props (e.g. `rule.remove_emojis = True`, `rule.blacklist = [...]`).
- `FilterRule(...)`, `ContentPolicy.from_dict({...})`, `CleanerRule(...)`, `MessagePayload(message_id=1, chat_id="-1001", text=..., media_type=MediaType.TEXT)`.
- `MediaType` from `core.domain.value_objects`.

Assert on the actual result type: `EvaluationResult.action/.reason`, `PolicyDecision.allowed/.reason`, a transformed string, or `AdvancedSettingsEngine` return values. Failure message must include fixture id + engine + expected vs actual. Skip cleanly (not fail) when a fixture id you don't know how to map appears, and print its id so the coordinator sees the gap. Use pytest parametrize over fixture records. Deterministic — no network, no DB, no sleeps.

## Task 3 — run and report
Run: `cd /root/AutoTelegramForward && /tmp/atfven/bin/python -m pytest core/tests/audit -q` (that venv has the deps; if unavailable try `/root/.venv-ltf/bin/pytest`). Then run `core/tests` wide enough to confirm no regression. Report: files changed, exact commands, pass/fail counts, any fixture whose expectation was wrong (fix it in the JSON only with a recorded reason — but coordinate with me first), remaining uncertainty.
