# Cross-feature coverage matrix (Part 2, STEER 01)

Canonical inventory of every feature audited in Part 1, with the entry points, authorization boundary,
persistence, worker, downstream operation and test evidence for each. Traceable IDs are `F01`–`F28` (the
Part 1 steers, in order) plus `X01`+ for features found in the repository that Part 1 did not name.

Part 1 per-switch evidence (what was wrong, how it was measured, what changed) lives in
`docs/task-feature-switch-matrix.md`; this document is the map that ties each feature to its identifiers, its
authorization boundary and its coverage. A feature is **not** claimed functional because a screen renders,
a route answers 200, or a row exists: the status column cites the test that exercises the real engine.

## How to read a row

| Column | Meaning |
|--------|---------|
| ID | Traceable identifier, stable across batches and PRs |
| Feature | Name as the user sees it |
| UI entry | Bot callback family / dashboard route that opens or toggles it |
| API / RPC | Go API route(s) and Python RPC method(s) that can change it |
| Authz | Where ownership is enforced for the write *and* the read |
| State | Table + fields that hold it |
| Worker | What actually executes it |
| Tests | The file(s) that prove it |

## Bot callback families (verified in `core/infrastructure/telegram/pro_bot_ui.py`)

`_cb_rule_*` (task detail and its switches), `_cb_reg_*` (`RegistryEditor`: advanced/all settings),
`_cb_clean_*` (cleaner), `_cb_filter_*` (filters), `_cb_rule_blacklist_*` / `_cb_rule_whitelist_*` (policy),
`_cb_rule_replace_*`, `_cb_rule_header_*` / `_cb_rule_footer_*`, `_cb_rule_delay_*`, `_cb_rule_draft_*`
(draft), `_cb_rule_ai_*` (AI rewrite + prompts), `_cb_rule_bot_token_*` (task bot token),
`_cb_rule_watermark_*`, `_cb_rule_translate_*`, `_cb_smart_*` (brand gate / smart routing),
`_cb_rule_route_*` + `_cb_route_*` (account routing), `_cb_ai_*` (AI configs), `_cb_pv_*` (PV responder),
`_cb_diag_*` (diagnostics + DLQ), `_cb_logs_*`, `_cb_adm_*` (admin), `_cb_migrate_*` + `_cb_backup`.

Callback payloads that carry a task or a resource are `prefix:<id>[:<arg>]` (`rtg:<id>:<state>`,
`rtx:<id>:<idx>`, `rtxd:<id>:<idx>:<version>`, `rdly_s:<id>:<sec>`, `rfl_bl:<id>`, `rge:<id>`, ...). Every one of
them is re-resolved against the repository with the authenticated `from_user.id`; a payload is a *selector*,
never proof of ownership.

## Go API routes (verified in `api/main.go`)

Auth: `/api/auth/{register,login,exchange-bot-token,tma,me,change-password,bot-login-link}`.
Dashboard (JWT-protected): `/api/rules`, `/api/rules/{id}` (+`/settings`, `/logs`), `/api/rules/settings/schema`,
`/api/rules/{id}/{duplicate,toggle,pause,resume,stop,restart,route-path}`, `/api/filters`, `/api/ai-configs`
(+`/check-key`, `/providers`), `/api/bot-tokens` (+`/{id}/defaults`), `/api/sessions` (+`/{id}/backup`,
`/{sessionId}/chats`), `/api/prompts` (+`/test`), `/api/settings`, `/api/client/settings` +
`/api/client/credentials`, `/api/stats`, `/api/overview`, `/api/tasks` (+`/{id}/{cancel,retry}`),
`/api/queue`, `/api/dlq` (+`/{id}/retry`), `/api/logs` (+`/export`, `/policy`, `/stats`,
`/diagnostics/{start,stop}`, `/purge`), `/api/audit/events`, `/api/ratelimit`, `/api/extensions`,
`/api/pv-responder`, `/api/simulate`, `/api/backup/{export,import}`, `/api/diagnostics`.
Admin: `/api/admins`, `/api/users` (+`/{id}/status`), `/api/broadcast`, `/api/tasks`.
Probes: `/healthz`, `/readyz`, `/api/version`, `/api/openapi.yaml`, `/api/system/{setup,env}`,
`/api/{,v1/}health/services`.

## RPC surface (verified in `proto/autoforward.proto`, 78 methods)

Rule lifecycle: `ListRules GetRule CreateRule UpdateRule DeleteRule TestRule PauseRule ResumeRule`.
Rule settings: `GetRuleSettings UpdateRuleSettings GetRuleSettingsSchema`. Filters: `ListFilters CreateFilter UpdateFilter DeleteFilter`.
AI: `ListAIConfigs CreateAIConfig UpdateAIConfig DeleteAIConfig TestAIRewrite CheckProviderKey ListAIProviders`.
Prompts: `ListPromptTemplates SavePromptTemplate DeletePromptTemplate TestPrompt`. Sessions: `ListSessions StartLogin
SubmitCode SubmitPassword BackupSession RestoreSession TerminateSession ResolveChat ListChats`. Delivery: `GetDeliveryStats
ListDeadLetter ReplayDeadLetter PurgeDeadLetter`. Accounts: `RegisterAccount LoginAccount ValidateToken IssueWebToken
ExchangeWebLoginCode IssueLoginCode ChangePassword GetAccount GetOverview ListUsers SetUserStatus Broadcast
GetSettings SaveSettings ListAdmins AddAdmin RemoveAdmin GetClientSettings SaveClientCredentials ListBotTokens
CreateBotToken DeleteBotToken GetBotDefaults SetBotDefaults`. Tasks: `CreateTask GetTask ListTasks CancelTask RetryTask`.
Logs: `GetLogPolicy SetLogPolicy QueryAuditEvents QueryLogs LogStats PurgeLogs`. Backup: `BackupExport BackupImport`.

## Tables (verified in `core/infrastructure/persistence/database.py`)

`sessions forward_rules filter_rules ai_configs audit_log api_credentials bot_tokens processed_messages ui_states
users error_log rule_stats metrics_hourly auth_flows message_map content_dedup delivery_jobs prompt_templates
prompt_versions dead_letter_queue web_accounts web_tokens app_settings channel_cursors administrators
broadcast_queue web_login_codes app_secrets user_tasks schema_version`.

`forward_rules` holds both the task and its inline configuration (`metadata` JSON carries the per-task option
groups, `filter_rule_id` the attached filter set, `owner_user_id` the tenant, `is_active` / `is_paused` /
`paused_until` the status). `message_map` is the source→destination mapping used by reply/edit/delete sync and
by scheduled deletion; `content_dedup` the duplicate-filter claim; `delivery_jobs` the durable queue.

## Feature rows

| ID | Feature | UI entry | API / RPC | Authz | State | Worker | Tests | Status |
|----|---------|----------|-----------|-------|-------|--------|-------|--------|
| F01 | Edit sync ON/OFF | `rtg_edit`/`rtg_fwdhdr` family | `PATCH /rules/{id}`, `UpdateRule` | `_owned_rule` + owner-scoped SQL | `forward_rules.metadata.sync_edits` | `SyncEngine.handle_edit` via `MessageDispatcher._route_edit` | `test_sync_switches_end_to_end.py` (Part 1) | PASS |
| F02 | Delete sync ON/OFF | same | same | same | `forward_rules.metadata.sync_delete` | `SyncEngine.handle_delete` | `test_sync_switches_end_to_end.py` | PASS |
| F03 | Reply ON/OFF | same | same | same | `forward_rules.metadata.sync_reply` | `MessageDispatcher._route_reply` (new) | `test_sync_switches_end_to_end.py` | FAIL → fixed |
| F04 | Header / Footer | `rhf:*`, `rtg_*_header` | `PATCH /rules/{id}/settings` | `_owned_rule` | `forward_rules.custom_header/footer` + metadata | `FilterEngine.transform_text` → `apply_header_footer` | `test_header_switch_end_to_end.py` | FAIL → fixed |
| F05 | Duplicate filters | `rtg_dup` | `PATCH /rules/{id}` | `_owned_rule` | `content_dedup`, `processed_messages` | `DurableMessagePipeline` DEDUPLICATE stage | `test_duplicate_filters_dedupe.py` | FAIL → fixed |
| F06 | Replace | `rtx:*`, `rtxa:*`, `rtxd:*:version` | `PATCH /rules/{id}` | `_owned_rule` + generation check | `forward_rules.replacements` | final-replacements authority in `_apply_transforms` | `test_replace_rules_end_to_end.py` | FAIL → fixed |
| F07 | Blacklist | `rfl_bl:*` | `PATCH /rules/{id}` | `_owned_rule` | `metadata.content_policy.blacklist_words` | `policy_chain` → `ContentPolicy.evaluate` | `test_blacklist_whitelist_end_to_end.py` | FAIL → fixed |
| F08 | Whitelist | `rfl_wl:*` | same | same | `metadata.content_policy.whitelist_words` | `ContentPolicy.evaluate` | same | FAIL → fixed |
| F09 | Cleaner | `_cb_clean_*` | `PATCH /rules/{id}/settings` | `_owned_rule` | `ForwardRule.build_cleaner()` | `CleanerRule.clean()` + link engine + `_clamp_entities` | `test_cleaner_options_end_to_end.py` | FAIL → fixed |
| F10 | Filters | `_cb_filter_*`, `rcl:` | `PATCH /rules/{id}`, `/filters` | owner check on the attached `FilterRule` | `filter_rule_id` + `filter_rules.owner_user_id` | FILTER stage → `FilterEngine.evaluate` | `test_filters_end_to_end.py` | FAIL → fixed |
| F11 | Set Delay | `rdly_s:<id>:<sec>`, `rdly:` | `PATCH /rules/{id}/settings` | `_owned_rule` | `forward_rules.delay_seconds` | advanced/legacy resolver + durable claim gate | `test_forward_delay_end_to_end.py` | FAIL → fixed |
| F12 | Task status | `rtg:<id>:<state>` | `/rules/{id}/{toggle,pause,resume,stop,restart}` | conditional UPDATE incl. owner | `is_active` + `is_paused`/`paused_until` | routing + pipeline + `DurableQueueManager` | `test_task_status_end_to_end.py` | FAIL → fixed |
| F13 | Duplicate task | `_cb_rule_duplicate` | `POST /rules/{id}/duplicate` | owner-scoped read then insert | new `forward_rules` row | n/a (config only) | `test_cross_tenant_boundaries.py` | FAIL → fixed (Part 2 batch A) |
| F14 | Rename task | `_cb_rule_rename` | `PATCH /rules/{id}`, `UpdateRule` | `_owned_rule` | `forward_rules.name` | n/a | Part 2 batch B | in audit |
| F15 | Draft | `_cb_rule_draft_*`, `_cb_rule_edit_draft` | `PATCH /rules/{id}/settings` | `_owned_rule` | `ui_states` / metadata | n/a | Part 2 batch B | in audit |
| F16 | Advanced settings | `_cb_reg_*` (`RegistryEditor`) | `/rules/{id}/settings`, `GetRuleSettingsSchema` | `_owned_rule` + per-key spec | `forward_rules.metadata` | whatever stage consumes the key | Part 2 batch C | in audit |
| F17 | All settings | `_cb_settings`, `_cb_reg_groups` | `GET/PUT /rules/{id}/settings` | `_owned_rule` | same store as F16 | same as F16 | Part 2 batch C | in audit |
| F18 | Delete task | `_cb_rule_del`, `_cb_rule_del_confirm` | `DELETE /rules/{id}`, `DeleteRule` | owner-scoped delete | `forward_rules` + dependants | queue cleanup | Part 2 batch B | in audit |
| F19 | Route: account | `_cb_rule_route_*`, `_cb_route_*`, `bot_route_token_*` | `/rules/{id}/route-path` | session ownership | `forward_rules.session_id` | dispatcher client pool | Part 2 batch C | in audit |
| F20 | Bot token | `_cb_rule_bot_token_*` | `/bot-tokens`, `create/delete`, RPC `CreateBotToken` | owner-scoped + write-only secret | `bot_tokens` | token attached to a task's route | Part 2 batch C | in audit |
| F21 | Categories | task-list filter buttons | `GET /rules` | caller-scoped query | `forward_rules.category`/metadata | n/a (list filter) | Part 2 batch D | in audit |
| F22 | Back to Rules | `_cb_rule_back_*`, `_cb_back` | n/a | resolves the task id via owner | n/a (navigation) | n/a | Part 2 batch D | in audit |
| F23 | Channel comments | task option | `PATCH /rules/{id}` | `_owned_rule` | metadata | delivery path | Part 2 batch D | in audit |
| F24 | Forward Every N | `_cb_rule_*` counter options | `PATCH /rules/{id}` | `_owned_rule` | `channel_cursors` / counters | pipeline counter | Part 2 batch D | in audit |
| F25 | Keyword confirmation | keyword callbacks | n/a (bot-local) | callback bound to task+user, expiring | callback payload | bot reply path | Part 2 batch C | in audit |
| F26 | Keyword reply threshold | `_cb_rule_*` keyword options | `PATCH /rules/{id}` | `_owned_rule` | keyword state | reply matcher | Part 2 batch D | in audit |
| F27 | Auto delete message | task option + interval | `PATCH /rules/{id}` | `_owned_rule` | scheduled deletion rows | scheduler / worker | Part 2 batch E | in audit |
| F28 | Outbound webhook | task option | `PATCH /rules/{id}` | `_owned_rule` | metadata | delivery hook | Part 2 batch E | in audit |
| X01 | AI rewrite | `_cb_rule_ai_*`, `r_ai:*` | `/ai-configs`, `TestAIRewrite`, `check-key` | owner-scoped config; key write-only | `ai_configs`, `forward_rules.metadata.ai` | AI stage in `_apply_transforms` | `test_ai_*.py` | PASS (Part 1 scope) |
| X02 | Watermark | `_cb_rule_watermark_*`, `rwm:` | `PATCH /rules/{id}/settings` | `_owned_rule` | metadata | watermark stage | covered by cleaner/transforms tests | PASS |
| X03 | Translation | `_cb_rule_translate_*` | settings | `_owned_rule` | metadata | translate stage | transforms tests | PASS |
| X04 | Schedules (auto/power) | `_cb_rule_toggle_*_sched` | settings | `_owned_rule` | metadata | scheduler | Part 2 batch D | in audit |
| X05 | Task bot-token attach | `_cb_rule_bot_token_*` | `bot-tokens/*/defaults` | owner-scoped | `bot_tokens` | `_bot_route` in the worker | Part 2 batch C | in audit |
| X06 | PV responder | `_cb_pv_*` | `/pv-responder` | owner-scoped | `app_settings` | PV responder worker | Part 2 batch E | in audit |
| X07 | Sessions / accounts | `_cb_sessions`, `_cb_login*`, `_cb_creds` | `/sessions`, `StartLogin`, `SubmitCode`... | session ownership; login challenge bound to the requester | `sessions`, `auth_flows`, `api_credentials` | dispatcher pool | Part 1 carried item: revoked session | in audit |
| X08 | Backup / migrate | `_cb_backup`, `_cb_migrate_*` | `/backup/{export,import}`, `BackupExport/Import` | owner-scoped export; import re-targets owner | all tables | n/a | `test_backup_service.py` | PASS |
| X09 | Diagnostics / DLQ | `_cb_diag*` | `/diagnostics`, `/dlq`, `/queue` | caller-scoped | `dead_letter_queue`, `delivery_jobs` | worker replay | `test_*.py` (dlq) | PASS |
| X10 | Logs & audit | `_cb_logs*` | `/logs*`, `/audit/events`, RPC `QueryLogs` | caller-scoped, redacting | `audit_log`, `error_log`, `rule_stats` | Rust logger | Part 2 batch E | in audit |
| X11 | Admin panel | `_cb_adm_*` | `/admins`, `/users`, `/broadcast` | admin id list server-side | `administrators`, `users` | broadcast queue | Part 2 batch D | in audit |
| X12 | Web dashboard / TMA | dashboard UI | `/api/auth/tma`, JWT routes | JWT + ownership per resource | `web_accounts`, `web_tokens` | same engine as the bot | Part 2 batch F | in audit |

## Cross-cutting requirements tracked per batch

* **Authorization** — every write in the table above must be refused for a second tenant with the same or a
  different task id (Part 2 STEER 02). Enforced in SQL (`owner_user_id` in the statement) or by resolving the
  resource through an owner-scoped read first; never from the callback payload.
* **Isolation** — settings, child resources, counters, drafts, mappings and cached objects belong to one task
  (Part 2 STEER 03); duplication must deep-copy and re-own.
* **Pipeline** — the order and interaction of the stages is documented in the Part 1 matrix and proved together
  in Part 2 STEER 05.
* **Delivery semantics** — the durable queue gives at-least-once delivery with a claim gate; idempotency keys
  and the release-on-failure rule are what keep it from becoming at-least-twice (Part 2 STEER 06).
* **Logging** — every refusal, retry and drop carries a `[PIPELINE:...]`-style structured line with the task
  scope, and no secret ever reaches a log line (Part 2 STEER 09).

## Known limitations carried into Part 2

* SQLite is the live store in this environment; PostgreSQL is the authoritative schema and is covered by the
  same repository contract, but no live PostgreSQL instance is available here to execute it end to end.
* `flutter`/mobile and the Rust logger are built and tested by CI; they are not exercised on this host.
* A→B end-to-end Telegram delivery cannot be observed from this environment (no bot membership in the two test
  channels), so delivery is proven at the worker/client boundary with a recording client.

## Part 2 STEER 05 — combined-pipeline findings

Three defects only appear when several features are configured **together**. All three were reproduced with the
real `DurableMessagePipeline` against a real SQLite store
(`core/tests/test_combined_pipeline_stages.py`, 13 tests) and fixed in the same batch.

The stage order the file pins, read off `_apply_transforms` and the `process()` stage records:

    INGEST → NORMALIZE → DEDUPLICATE → FILTER (media gates + content policy)
           → TRANSFORM (cleaner → link engine → emoji → task policy → AI → replacements → header/footer → watermark)
           → ENQUEUE (per-target override, then durable job per target)

| ID | Defect | Root cause | Fix | Evidence |
|----|--------|-----------|-----|----------|
| D1 | A task whose header/footer (or a replacement's target text) contained one of the task's **own** blacklist words rejected **every** message it ever forwarded — silently | the content policy was decided twice: once in FILTER on the source text, and again at ENQUEUE on `ctx.transformed_text` (which already includes the task's own header/footer) | the ENQUEUE gate now decides only `target:`-scoped overrides; the task's policy and any `source:` override stay single-authority in FILTER | `test_a_word_the_tasks_own_header_introduces_is_not_filtered`, `test_a_target_override_still_rejects_only_its_own_target`, `test_a_target_blacklist_is_not_replaced_by_the_task_decorations` |
| D2 | Two tenants watching the same source channel: only **one** of them ever forwarded; the other's task was starved permanently | `SmartRoutingEngine.matching_rules` applied the single-match policy across *all* rules, and its sort key (`priority`, `execution_order`, `created_at`) is tenant-controlled — so one tenant could starve another's tasks at will | single-match is now applied **per owner**: one message is consumed by exactly one task of a tenant (unchanged), and every other tenant's task still gets it | `test_two_tenants_tasks_deliver_to_their_own_targets_from_one_message` |
| D3 | A paused task's drop was logged and audited as "none of N active rules matched" (`RULE_NOT_MATCHED`) | `evaluate_rule` returns `rule_paused` for a paused rule, so the rule never reaches `matching`; the pause-aware branch below it was unreachable | when every candidate for the source is paused, the drop is recorded as `rule_paused` / `RULE_PAUSED` with its own log line | `test_a_paused_task_is_reported_as_paused_not_as_no_rules` |

Two further invariants the same file pins, so a later change cannot quietly break them:

* a blacklisted word is blocked **before** any transform: text the cleaner would have removed still counts
  (`test_a_blacklisted_word_is_blocked_even_when_the_cleaner_would_remove_it`);
* a message that cleans/rewrites down to nothing is dropped with `empty_after_clean` — not delivered as an
  empty message, not dropped silently (`test_a_message_that_cleans_to_nothing_is_dropped_with_a_reason`);
* one replay of the same event is deduped (`duplicate_event`/`duplicate_content`), and a reply whose parent was
  never forwarded is still delivered without inventing a mapping.

Task→session ownership is enforced where the session is chosen: `ForwardRuleUseCases.create`/`update` raise
`PermissionError` for a session owned by someone else, and the rejected task is not stored
(`test_creating_a_task_on_another_tenants_account_is_refused`).
