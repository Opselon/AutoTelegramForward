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
| X05 | Task bot-token attach | `_cb_rule_bot_token_*` | `bot-tokens/*/defaults` | owner-scoped at the write (`_guard_route_owner`), **and now at delivery** (`BotTransportCache._may_use`) | `bot_tokens` | `_bot_route` in the worker | `test_bot_route.py` (5 new), `test_bot_defaults_worker.py` (2 new) | **FAIL → fixed** (Part 2 batch D): the token id is a routing identifier that travels in the rule metadata and the job payload, and the worker resolved it with an **unscoped** read (`get_by_id`) — so a task whose metadata named another tenant's active bot delivered through that bot's credential and inherited that bot's defaults (a configuration leak and an impersonation). The rules-write guard (`_guard_route_owner`) already refused a foreign id at attach time, so this was reachable only through a row written before the guard existed or edited outside the API — reachable is reachable. Now: the worker passes the task's owner, the cache compares the owner on **every** call including a cache hit (a pool keyed only by token id was the second half of the hole), a foreign or ownerless row is not found, and the refusal is indistinguishable from \"no such token\" so an id cannot be probed. `attached_defaults` derives the owner from the rule itself, so a caller that forgets the argument cannot silently fall back to an unscoped read. Evidence: two tests fail on the pre-fix revision and pass after it |
| X06 | PV responder | `_cb_pv_*` | `/pv-responder` | owner-scoped | `app_settings` | PV responder worker | Part 2 batch E | in audit |
| X07 | Sessions / accounts | `_cb_sessions`, `_cb_login*`, `_cb_creds` | `/sessions`, `StartLogin`, `SubmitCode`... | session ownership; login challenge bound to the requester | `sessions`, `auth_flows`, `api_credentials` | dispatcher pool | Part 1 carried item: revoked session | in audit |
| X08 | Backup / migrate | `_cb_backup`, `_cb_migrate_*` | `/backup/{export,import}`, `BackupExport/Import` | owner-scoped export; import re-targets owner | all tables | n/a | `test_backup_service.py` | PASS |
| X09 | Diagnostics / DLQ | `_cb_diag*` | `/diagnostics`, `/dlq`, `/queue` | caller-scoped | `dead_letter_queue`, `delivery_jobs` | worker replay | `test_*.py` (dlq) | PASS |
| X10 | Logs & audit | `_cb_logs*` | `/logs*`, `/audit/events`, RPC `QueryLogs` | caller-scoped, redacting | `audit_log`, `error_log`, `rule_stats` | Rust logger | Part 2 batch E | in audit |
| X11 | Admin panel | `_cb_adm_*` + the `admin_add_id` / `force_sub_channel` text steps | `/admins`, `/users`, `/broadcast` | admin identity server-side (`_is_admin` on the handler, and now on the **renderer**) | `administrators`, `users`, `app_settings`, `broadcast_queue` | broadcast queue | `test_admin_boundary.py` (6) | **FAIL → fixed** (Part 2 batch E): 14 of the 15 admin callbacks gated on `_is_admin`, and the two admin text steps did too — but `admbcx` (broadcast *cancel*) had **no gate**: it cleared the caller's own UI state and then rendered the admin panel, so a non-admin got the install-wide body — total users, active tasks, dead-letter count, the forced-subscription channel — for one click. `_render_admin_panel` itself trusted its caller, which is why one ungated button was enough. Both fixed: the button is gated like its siblings, and the renderer now refuses a non-admin (logging `[ADMIN] refused …`) so a future handler cannot reopen it by forgetting one call. The suite presses every admin payload as a tenant through the real registered handlers and requires a **visible** refusal (silence is a finding), then checks the destructive ones changed nothing: the user was not suspended, no broadcast row was queued; and as the admin, the same press still works |
| X12 | Web dashboard / TMA | dashboard UI | `/api/auth/tma`, JWT routes | JWT + ownership per resource; browser-side identity is re-proven when the shared token changes | `web_accounts`, `web_tokens` | same engine as the bot | `sessionIdentity.test.mjs` (8) + `test_ui_state_consistency.py` | **FAIL → fixed** (Part 2 batch F): a second tab's login/logout was invisible to a mounted dashboard (it kept rendering one account while its requests were authorised as another), and adopting a different account reused the mounted React tree — both fixed: `storage` re-verification + an account-keyed `<App>`; the bot-side UI state store also never resumed after a restart and let a stale durable copy overwrite newer edits (D6/D7) |

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

## Part 2 STEER 06 — queue, concurrency and idempotency findings

The durable queue is where "forwarded once" can quietly become "never". Part 1 proved the keyword-reply feature
by asserting that the *enqueue call* carried `job_kind="keyword_reply"` with the queue stubbed out — a test that
could not fail no matter what the store did. Driving the real `DurableQueueManager` against a real SQLite file
(`core/tests/test_queue_concurrency_and_idempotency.py`, 15 tests) and the migration against a hand-built
pre-v20 table (`core/tests/test_delivery_jobs_migration.py`, 5 tests) found two defects that made the reply
impossible to deliver.

| ID | Defect | Root cause | Fix | Evidence |
|----|--------|-----------|-----|----------|
| D4 | **Keyword reply / keyword confirmation never reached a target.** The reply job was refused by the queue on every message that was also forwarded — i.e. always — and nothing was logged, while the pipeline logged `keyword matched … reply enqueued` | `delivery_jobs` deduped with `INSERT OR IGNORE` on `UNIQUE(rule_id, source_chat_id, source_message_id, target_chat_id)`. A reply is a *second deliverable to the same target for the same source message*, so its row matched the forward's key, the insert was ignored and `enqueue_delivery` returned `False`. The distinct job *id* the code added could not help: the UNIQUE key never looked at the id. The pipeline ignored the return value, so the refusal produced no log at all | the deliverable kind is now part of the queue row's identity: `job_kind` is a column and part of the UNIQUE key (SQLite: `_rebuild_delivery_jobs_deliverable_key` — copy rows → compare counts → swap inside a SAVEPOINT, so a failure leaves the old table and its jobs untouched; Postgres: `scripts/postgres/schema.sql` widens `delivery_jobs_unique`). The pipeline now checks the result and logs `[PIPELINE:KEYWORD_REPLY] … refused by the queue` | `test_a_keyword_reply_is_not_deduplicated_away_by_the_forward`, `test_the_pipeline_stores_both_deliverables_for_a_keyword_match` (real pipeline → 2 rows: `forward` + `keyword_reply`), `test_the_old_collision_can_no_longer_happen`, `test_a_legacy_table_is_rebuilt_with_its_rows_intact`, `test_reopening_a_migrated_database_changes_nothing` |
| D5 | A reply's outcome **overwrote the forward's delivery record**: edit/delete sync was pointed at the reply post instead of the forwarded copy, and a failed reply marked an already-delivered forward as `RETRY_WAIT`/`FAILED` | `message_map` is keyed by (rule, source, msg, target) too, and the worker wrote `target_message_id` + `delivery_status` there for **every** job, replies included. The reply is a different deliverable sharing that key | only the primary forward owns the mapping row: `DurableQueueManager._note_mapping` skips non-forward deliverables (all five post-dispatch writes route through it), and the enqueue path only upserts the mapping for a forward | `test_a_reply_outcome_never_rewrites_the_forwards_mapping`, `test_a_reply_does_not_create_a_mapping_row_of_its_own` |

Invariants the same file pins so a later change cannot quietly break them:

* one job, one delivery — a live lease cannot be claimed by a second worker, an expired lease is reclaimed as
  **the same job** (never a forked row), and a destination is a single lane drained in source order;
* nothing is lost — a `RETRY_WAIT` job waits its backoff and comes back, an exhausted job lands in the
  dead-letter queue **with its payload**, and a job claimed by a crashed worker is recoverable;
* the dead-letter queue is scoped to the caller's own rules, and an empty scope matches nothing;
* a pending job survives a reopen with its text and every field the worker needs to finish it.

## Part 2 STEER 08 — state and cache consistency findings

The UI state store is the only durable trace of a user's flow, and the dashboard is the only place a *second*
tab of the same browser can see someone else's session. Both had gaps. The Python findings come from driving
the manager against the **real** `SqliteUiStateRepository` (`core/tests/test_ui_state_consistency.py`, 7 tests);
the browser findings from `web/frontend/tests/sessionIdentity.test.mjs` (8 assertion groups, run by the
`Session identity rules` CI step exactly like the repo's other framework-free frontend rules).

| ID | Defect | Root cause | Fix | Evidence |
|----|--------|-----------|-----|----------|
| D6 | **The bot UI never resumed a user's flow after a restart** — the screen step and the navigation stack were lost, so `Back` after a redeploy was a guess, despite the module's documented promise | `UiStateManager._ensure_loaded` called `await self._repo.load(uid)`. The real interface (`IUiStateRepository` → `SqliteUiStateRepository`) exposes `get()`, so every call raised `AttributeError`, which the `except Exception` directly above swallowed into `row = None`. Nothing logged it. The existing suite could not catch it because its `FakeRepo` implemented `load()` — a method no real repository has, i.e. the double was written to the manager's assumption instead of to the interface | the manager reads the interface method `get()`; it accepts both payload shapes (a versioned v2 payload, or a pre-manager row where the column *was* the buffer); a load failure logs `[UI_STATE:LOAD_FAILED]` and is deliberately **not** cached, so a transient failure does not disable resume for the process's life. `FakeRepo` in `test_ui_state_manager.py` was corrected to the interface | `test_the_manager_resumes_through_the_real_repository_interface` (real repo, real SQLite: step **and** stack survive a new manager), `test_a_load_failure_is_reported_and_does_not_poison_the_cache` |
| D7 | A durable copy that **trailed the live state overwrote newer edits** on the next press (the exact failure the module exists to prevent) | `_ensure_loaded` took its early-return branch when the user was *already* loaded and set `self._loaded[uid] = False` in that branch — so the following call re-read the row and replaced the live object. Every mutation path calls `_ensure_loaded`, so any stale persisted copy (a persist that failed and was swallowed, a replica, a lost write) silently won the next interaction | one load per process per user: an already-loaded user returns immediately, and the row is never applied over state the process already holds | `test_a_loaded_user_is_not_reloaded_on_every_touch` (5 touches ⇒ 1 read), `test_a_stale_durable_row_cannot_overwrite_live_state` (row rewritten to an older payload; the live step survives) |
| D8 | **A second browser tab could keep rendering one account's dashboard while its requests were authorised as another's**, and adopting a different account did not remount the tree | the session token lives in `localStorage`, shared by every tab, but a tab proved its session once, on mount, and nothing re-verified when another tab logged in, logged out or switched accounts. Separately `<App me={session.me} />` had no `key`, so React reused the mounted tree across identities and kept every fetched list and page on screen until it happened to be refetched | a `storage` listener re-proves the session when the stored token changes (including `localStorage.clear()`, where `key` is `null`), and the dashboard is keyed by the account (`appKey(me)`), so a different identity remounts and drops cached state. Session rules live in `src/sessionIdentity.ts` (module scope, framework-free, run by node); the logout button now calls session-core instead of duplicating the storage-key literal | `sessionIdentity.test.mjs`: number/string ids name the same account, two accounts never compare equal, nonsense ids name nobody, the React key is stable per account and distinct across accounts, and unrelated storage keys/malformed events are not session changes |

Invariants the same files pin: state is per **verified** user (`reset` on one account leaves another's flow and
row intact); a resumed stack is seeded once and can never rewind a live one; a step naming a task that has been
deleted refuses instead of acting (`test_a_deleted_task_cannot_be_reached_through_stale_ui_state`).
