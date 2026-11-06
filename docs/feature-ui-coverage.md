# Feature → UI reachability

How to regenerate: `python3 scripts/audit_feature_ui_coverage.py` (`--gaps`, `--json`, `--surface web`).

The tool searches each surface's real source for the vocabulary a feature cannot be exposed without
(`edit_sync`, `watermark`, `webhook`, …). It answers *"can a user reach this?"*, not *"does the engine do it?"* —
that second question is what `docs/feature-coverage-matrix.md` answers with its test citations.

## Today (master `cd8d804`)

```
bot:    38/40 features reachable  — missing F26 (keyword reply threshold), F27 (auto delete)
web:    39/40 features reachable  — missing F14 (rename task)
mobile: 36/40 features reachable  — missing F14 (rename), F15 (draft), F26, F27
```

Read these carefully before fixing anything:

- **The web and mobile clients render the task-settings registry generically.** The dashboard labels 83 settings
  (`web/frontend/src/settingLabels.generated.ts`, ten locales each) and the mobile settings sheet renders whatever
  `/api/rules/settings/schema` returns, so a feature such as `auto_delete_seconds` or `keyword_reply` *is* reachable
  even though no dedicated control exists. The F26/F27 "missing" rows for web/mobile are therefore
  **vocabulary misses, not user-facing gaps** — the setting is there, under its registry row.
- **F14 (rename) is a real gap** on web and mobile: there is no rename affordance in either client, so a task can
  only be renamed from the bot. That is the one feature-parity defect worth a ticket.
- **X13–X16 (branding channel gate, link/@ replace maps, prompt library)** were added with
  dedicated surfaces on all three clients: the bot's smart-menu gate toggle and `rmap:` panel
  (`replace_maps_bot_ui.py`) plus the global prompt manager (`prompt_bot_ui.py`), the dashboard's
  branding checkbox and replace-map textareas in `RuleModal`, and the mobile prompt section with the
  registry-driven settings sheet. All new copy is catalogued in the ten locales on every surface
  (bot translations.json, web `locales/*.ts`, mobile `app_*.dart`), and the audit's bot corpus now
  includes the two new bot modules.
- **F15 (draft)** is bot-only by design today: drafts are a per-conversation FSM concept on the bot side; the
  dashboard has no draft workflow. Keeping it bot-only is acceptable until drafts become a server-side object.

## What "reachable" must mean going forward

A feature counts as reachable on a surface only when all three hold:

1. A control exists (dedicated button, row, or a registry row the client renders).
2. The control is labelled in all ten locales (no raw server key on screen).
3. A test exercises the control end to end (widget test, component test, or bot pytest) — not just a rendered label.

The audit checks (1) heuristically and cannot check (2) or (3); the UI-copy gates and the test suites cover those.
