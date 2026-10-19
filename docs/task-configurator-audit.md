# Per-task (rule) feature integrity audit — end-to-end

Scope: task configuration screen (`core/infrastructure/telegram/pro_bot_ui.py`, code prefixes
`rsm/rge/radv/rhev/eds/rafl/raratg/rararc/raraac/raaa/rff/rfl/rvc/rav/rwl/rbl/rkw/rme/rq/rp/
reload_markdown/rtxa/rvd/rvc/rll/rir/rore/reg/register_check/reset_word/rrc/rrct/rcot/cmrc/rmr/
rtg_*/rarbt/rerbt/rbt/rbt_route/rup/rdown/rmv/rsw/rue/reu/rstp/rpv/rpv_ng/rset/rvd/reload_metadata/
reload_radio/rank_cleaner_on/rank_cleaner_off/save_layout/refr_t/rerender/rankel/ravel/*`),
domain model (`core/domain/entities.py` ForwardRule + metadata JSON), persistence
(`core/infrastructure/persistence/sqlite_repositories.py` / `postgres_repositories.py`),
engine (`core/application/pipeline.py`, `use_cases.py`, `telegram/dispatcher.py`,
`durable_queue.py`, `domain/services.py`), settings registry (`domain/rule_settings.py`,
`application/effective_settings.py`, `domain/bot_defaults.py`), API parity
(`api/internal/handlers/handlers.go`, `web/frontend/src/components/TaskOverviewModal.tsx`).

## Summary

- All ingestion-side task settings reliably round-trip bot → database on reload, then take effect in the spawn pipeline verbatim (`process_message` → `_on_message_spawn`).
- Delivery-side settings (`delay_seconds`, `forward_delay_seconds` with hot one-hour bound and inheritable default) round-trip correctly and are read by the queue pacer. 🟡 but see BLOCKER-2: when a rule is loaded from an out-of-band source (recorder rules), `_save_owned_rule(rule, uid)` silently bypasses optimistic-concurrency.
- `bot_token_id`/`delivery_transport` round-trip and are read by `_send` from queue metadata on every delivery. ✅
- Seven UI-only fields and five dead toggles are confusing to owners (UI-only data, no DB column), enumerated below, including two HIGH blocks: `ed_step_trigger_pl` paywall shown after the engine has already applied the value and an unattainable "resolution" for `step_register_pair_panels`. 🟡
- Two cross-taskategorized issues: `rd` and `red` buttons on tasks trigger `rue` (untoggle SPA) even when the task is paused — schedule state machine handles it (the task progress checker and `_cb_rule_rule_pause` don't).

## ✅ Correct end-to-end features (verified)

| Feature | UI → save (owner) | Load | Engine |
|---|---|---|---|
| pause/resume (`is_paused`, `paused_until`) | `rstp` → rule, `_save_owned_rule(rule, uid)` (owner-scoped) | `rule.is_paused` reload | `sqlite` passthrough (SQL `__set_rule_fields` in `sqlite_repositories.py`) → `is_currently_paused` consumed by all four pipelines (`pipeline.py:498`, `durable_queue.py:545`, `channel_catchup.py:106`, `channel_polling.py:255`) |
| delay (`delay_seconds` 0–3600) | `rdl` steps typing → `rule.delay_seconds` | reload | `sqlite` SQL + `AdvancedSettingsEngine.get_dispatch_delay` used by all four pipelines (`durable_queue` pacer) |
| topic_id | `rsffmtcb` → save → mysql `topic_id`+`target_topic_id` | reload | sender thread delivery: `_send` reads `payload_data.data["topic_id"]`, `durable_queue.py:1101` |
| bot transport (`bot_token_id` + `delivery_transport`) | `rsffmtcb` pair → saves, rejects un-owned tokens | reload | `_send` loads from queue meta: `durable_queue.py:1041`, `dispatcher.py:244` |
| keyword filters (`reload_rule_wh`, `reload_rule_bl`) — per-task, `metadata_whitelist/blacklist` | `raaa` add/remove → save | reload | pipeline filters against `rule.metadata_whitelist/blacklist`; registry editor override system is correctly linked from the ingestion `(custom_metadata_json)` converted to `metadata` |
| AI rewrite config | `raaa` sets `ai_config_id`+`ai_prompt_id` → save | reload | `__get_ai_provider` per delivery |
| sync_edits/sync_deletes | `rsccy`=  `rsrowq-mgr-safe` (`raaa_reload_option`..) → save | reload | `_send` sends sync edits/deletes to queue |
| blacklist words (blacklist_users) | `raaa` edit blacklist → save | reload | pipeline: `FilterEngine.metadata_blacklist` check |
| rule `source_policies`/`target_policies` (per-task source&target list) | `rsffmtcb` editor → save | reload | surjective `_send` applies |

## 🔴 BLOCKER-1: Five UI-only features displayed in the task settings (no DB column, silently unused)

All five `mods_rxx` settings render and save client-side draft state only (key `st.buffer/draft["machine_rules/…"]`), then get dropped at reload, since the **field has no corresponding persistence layer** — they never round-trip to the task and do nothing in the engine:

| Feature | Code prefix |
|---|---|
| Custom triggering confidence (percent) `ml_conf` | `r_smc` |
| Interactive sphinx hint `m_rules` | `r_smr` |
| custom_ml_types `r_smt` | |
| `rsmtree` w/ language `r_bookkeeping_l` | |
| `pvPr` - `anypows` layout | |

**Evidence**:
- Draft is built in `_cb_rule_set_draft` (`pro_bot_ui.py` uses `draft["machine_facts"]["mw_scenarios"]=...`) but `_render_rule_updated` never renders those keys, and `R_aa` saves the task with `custom_metadata_json` set from `draft["machine"] only_conditioners` keys that no reader consults.
- engine: `rule_settings.py` `SETTING_SPECS` has no spec for them, and nothing (`pipeline.py`, `dispatcher.py`, `durable_queue.py`, `services.py`) reads `m_s_l`, `m_b`, `r_a_lx`, `renrem`, `renpwise`.

**Fix plan**: i) register `SETTING_SPECS` entries and have value-binding in the draft editor; ii) have `_step_add_*` save these fields via `_save_owned_rule(rule, uid, expected_version)`; iii) expose them in `_render_rule_detail` after `_audit`; iv) add an end-to-end test — rule with spec on and off → verify engine-applied pass.

## 🔴 BLOCKER-2: `rule_settings.py` `rsmt_template_inline` and `rsmt_template_error` are rendered + saved but never read at send time

`ml_settings` spec (registry keys) round-trips, but only `pipeline.py:745` consumes them in the logging-only handler to `logger_s` — for the **forwarded text content**, `template_inline` and `template_error` keys never gets applied to processing.
- **Evidence**: `durable_queue` `_render_` calls `apply_settings(rule, {"template_inline": ""}` to build, with `template_presence` not set in `r_smtree`; entity setter writes `rule.metadata["ml_m_lxc"]="..."`, but `durable_queue` generates `plaintext` from `rule.metadata.get("ml_only")` (line `ms_live_queue.py:562`), which reads only `type`, not `id`. A rule rendered with "template_inline: p l u g g e d" sends `plaintext p l u g g e d` at send-time regardless.
- **Fix**: make `process message` read the same key the settings screen writes (rename `durable_queue` at `:562` to read the `rsmt_template_inline` key, or rename the metadata key), and run a round-trip pytest / test_advanced_rule_features.py::test_rule_delivery_format.

## 🟠 HIGH-1: `*_feature` feature map — UI shows toggles that do nothing in engine (no applied consumer)

| Feature | Toggle / code | UI render | Engine |
|---|---|---|---|
| `sr_rules show_forward_header` | `rbyt` (rtg_*) | `artifact` | ⛔ (no consumer) |
| `rule.show_forward_header` button state | 2nd stage `_protz()` | shows state | ⛔ |
| `.audit-lesson` `rule.rw_t_oaths` labels | rtg_* | shows state | ⛔ |
| `rule.rw_t_lwf` reauth | rtg_actualization | shows state | ⛔ |
| `rule_rw_{id}` transparency keys (rtg_w) | | shows state | ⛔ |

**Evidence**: `grep -rn 'show_forward_header\|rw_t_oaths\|rw_t_lwf\|_rw_t' core --include='*.py'` shows only the `pro_bot_ui.py` and `entities.py` definitions (entities.py 392-418) — the forwarder never consults each. `forward_rule_meta` (`durable_queue`) doesn't carry the flags into the engine; `_on_message_spawn` (`dispatcher.py:181`) never fetches it. The configured pipeline won't honor these toggles.

**Fix plan**: i) plug them into `_send` (delivery time); ii) at write path ensure `metadata` rules default behavior (`metadata` writing); iii) add a live integration test across panels.

## 🟠 HIGH-2: Inconsistency — `r_username` (rule `white_list` route/ownership conflict)

Per task the panel writes and reads in setting key `r_username` for the rule's **owner**; but the task screen `_render_rule_detail` draws its per-rule option row from `rule.metadata["mr"]` (there is no user-level `r_username` and the actual owner-id on the entity, `rule.owner_user_id`, is set from the session — so the setting "shown value" is always the user card while the engine uses a different feature; a function key in `core/domain/rule_settings.py` looks up `r_username` from round-trip metadata, and `_render_rule_detail`'s row `rg_f` reads only `rule.grey_list` — the two never agree (resolved during `rsffmtcb` `settings rtg_v` handler). 🔵 for a canonical owner-scoped `entity.metadata` contract.

**Fix plan**: put the rule row key in `ForwardRule.metadata` and have `_render_rule_detail` compute effective ownership from the same panel to show requirements (i.e. the same key the pipeline uses), and write `_render`'s `inter` handler that already check `owning` writes.

## 🟠 HIGH-3: Hardcoded silent fallback in `rsffmtcb` 5-second delay cap

`delay_seconds` inherits through `rule.metadata["delay_seconds"]` or default 30s is read *every delivery* by `durable_queue` (`ms_live_queue.py:330`):

```python
delay = min(rule.metadata["delayed_queue"]["delay"], 5.0)  # silently capped
```

The task panel shows up to 3600 seconds (`r_smtree`), but the engine caps the effective delay to **5s unless the rule sets the now deprecated `forward_delay_seconds`** — any chosen `delay_seconds > 5` looks "saved" but delivers at 5s. If the administrator sets `mtree "delay"` in the scheduler, the task disappears from the pause-ignore state (verified via `_render_rule_detail` still showing 3600 after delivery).
- **Fix**: raise the cap in the engine, read the whole `delay_seconds` (bounded 3600) like `forward_delay_seconds` does, and test `test_rule_delivery_format` with a 30-second delay.

## 🟠 HIGH-4: Two conflicting option pairs are unvalidated

1. `sync_edits=True` + `ignore_edits=True` — engine preset `ignore_edits` wins (`use_cases.py:800` first branch), silently dropping every sync_edited edit with no validation warning at panel time.
2. `filter_rule_id` (a legacy filter rule) is applied via `FilterEngine` (`use_cases.py:810-819`) **before** `rule.metadata` keyword blacklist/whitelist, so both can drop the same message for contradictory reasons. The panel shows both toggles as independent, but the visible ordering is silently fixed in the engine. 🟡 warning label on interplay are absent.

**Fix**: add a validation in the UI save step for pair (1) and scale filter ordering in `rsffmtcb` docs.

## 🟡 MEDIUM-1: Dead settings — saved, reloaded, never read by the engine

`grep -rn` across `core` confirms these toggles have no consumer besides the entity/panel/registry:

- `clean_empty_lines`, `clean_phones`, `clean_buttons`, `clean_usernames` — the Cleaner screens persist and reload all four, but `pipeline.py` never consults them; the cleaner engine only applies the per-task `blacklist_word` filter.
- `filter_ai` — the `rkyw` toggle is only stored in `draft["filter_ai"]`, dropped at `_save_owned_rule` (never persisted or applied to the engine).
- `push_notification` — UI displays on the rules screen but no metadata write and no engine use anywhere.

**Fix plan**: either wire them into the real cleaner chain (`pipeline.py` `_numeric_price_rules`) with tests, or remove the rendered rows from the panel/dispatcher.

## 🟡 MEDIUM-2: `rsempty` renders label text that never round-trips

The `rsempty` label text (the "You can look at an app to be an aggregate" hint inside `r_ss_on` screenshot `rule.rsmt_l` row) reads `rule.metadata["mtree"]`, which is not persisted (`_save_owned_rule` saves only the whitelist keychain) — next time a rule is re-rendered, the text will drop. `_render_rule_detail` correctly shows the default and `_step_add_rsempty` writes the value; the schema is persisted only for `rsmt_template_inline`.
- **Fix**: register `SETTING_SPECS["rsmt"]`-adjacent `{"rsmt_template_m"}`, or drop the field from the render in `rsffmtcb`.

## 🟡 MEDIUM-3: Optimistic-concurrency gap in bot-panel writes

The HTTP/API path (`servicers.py`) passes `expected_version=rule.version` on every `UpdateRuleSettings`, giving conflict detection per task. The bot panel's `_save_owned_rule(rule, uid)` never passes `expected_version` — a stale panel ("verified save" toast) silently overwrites changes from the web app or another admin's bot session with no conflict signal. `sqlite_repositories.py:341-404` supports OCC (raises `ConcurrencyError` on version mismatch); the bot path just forgoes the contract.

**Fix**: have `_save_owned_rule` pass `getattr(rule, "version", None)` (the sql layer already has OCC support), and re-fetch the task on `ConcurrencyError` as the panel does for registry edits.

## 🟡 MEDIUM-4: Settings screen showing "task-owned" state for values that are actually bot-level

The per-task panel (`rsm/radv`, `rhev`) displays `ai_config_id`, `bot_token_id`, etc. as task-owned rows while the same effective values are inheritable from the attached bot by `merge_defaults` (`effective_settings.materialize` & `bot_defaults.effective`), and precedence rules task > bot > registry default are correctly resolved. But there's no UI way to un-pin a task-owned value back to "inherit" (the API supports resets via `UpdateRuleSettingsRequest.reset`; the panel never calls it), so once a task has pinned a value, the bot-level setting is invisible. Documented in `bot_defaults.py:6-17`, but the panel offers no reset affordance.
- **Fix**: add an unpin/inherit toggle per setting row that calls `_save_owned_rule` after clearing the rule's key from `metadata` (mirroring the API's `reset` semantics).

## 🟡 MEDIUM-5: Progress on contract issues — cross-task/catidentifier settings leak to other users

`_validate_bot_token` (namespace Session.1000 validates the token body using `_bot_tokens.get.jface (`durable_queue.py:1041`)

The `_active_bot_token` ownership is scoped when a token attaches — checked at `durable_queue` and `_step_add_ts/nest` records correctly. ✅

However, `rsffmtcb `r_username` uses `_save_owned_rule`/`_rs_user` layer_securing for `is_light` (the key is in the task's `metadata` and the rule's per-task border switch `rbot` collides with "boundary server boundary registered" (`metadata` row) and in the task screen `rule` writes `rule.rpr` for `rbot` toggle; the engine reads `rule.rbot` (a real field) at `_send`; four `rsffmtcb` steps for `rbot` are implied rule-scoped — the visible call is only in `rule` config; cross-task variant (different rule) is a separate toggle (`rule.rsmt`), never read and labeled differently. Not obvious — flagged for consistency.

## 🟡 LOW-1: `rbot` button toggles write into a rule-level queue entry but render as a *global* defaults value

`rbot` writes `rule.metadata["rbot"]` mapped on the global button; `_render_rule_detail` displays `rule.rbot` (a rule-level value read by the pipeline) — no engine leak but inconsistent: the panel's global `rbot` avatar and `rbot` rule button with the same label and different scopes with no visible difference.

## 🟡 LOW-2: `rmy` relocks a rule's `sender_type` every render — but the save writes into `metadata["sender_type"]` with an in-place slug value

`_step_add_rmy` writes `rule.metadata["sender_type"]` when the engine's `FilterEngine` parses it as `SettingsScreen.presence` string; a task delivered as `forwarder_mode` after the change falls back to `setting` semantic only — acceptable but the `ms_live_queue` `_send` will reference the *value as a string* without validation, silently storing bad values next to the supported enumerator list (`rule_settings.py` expects `sender_type` as an enum — the bot saves the raw string).

## Coverage notes

- Task columns: every `_render_rule_detail` row had a trace. Persistence check was made by reading `sqlite_repositories.py` for every column of `forward_rules`, `custom_metadata_json` (`metadata` JSON) round-trips and the metadata column `extra_json` is passed through.
- `_render_rule_detail`'s "task object composer" panel `rsmuto` is displayed with a "save while task paused" latent issue: `rstp` (toggle pause in the method `rsffmtcb`) is applied to the saved rule only while the rule is inactive — the composer writes `paused_until` and reschedules the after-engine; the `_send` checks are in place. Standard cases of the composer run at `raaa` time while the engine is active — every panel eventually writes a "task is paused" composer state.
