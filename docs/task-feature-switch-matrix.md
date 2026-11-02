# Per-task feature switch matrix

Statuses are **evidence-based**: a switch is `PASS` only when the forwarding
engine's real behaviour matches the label — never because the screen renders, a
database value exists, or unit tests are green. Evidence quoted below was
measured against a running stack (`sha-9aeb96d`, 5/5 `atf-*` containers healthy)
and/or by driving the real engine through its real entry point.

Legend: `PASS` behaviour verified · `FAIL` confirmed defect · `PARTIAL` some
paths unverified · `NOT_IMPLEMENTED` no engine support · `PENDING` not yet audited.

| # | Switch | Status | Engine entry point | Evidence |
|---|--------|--------|--------------------|----------|
| 01 | Edit ON/OFF | PASS (was PARTIAL) | `SyncEngine.handle_edit` via `MessageDispatcher._route_edit` | Six-row truth table on the deployed image; see below |
| 02 | Delete ON/OFF | PASS | `SyncEngine.handle_delete` / `handle_delete_without_chat` via `MessageDispatcher` (DELETED_MESSAGES) | On/off, unmapped, cross-rule isolation, flood retry — `test_sync_switches_end_to_end.py` |
| 03 | Reply ON/OFF | FAIL → fixed | `SyncEngine.handle_reply` via `MessageDispatcher._route_reply` (**new**) | The switch had **no production caller**; a reply was never mirrored |
| 04 | Header NO/YES | FAIL → fixed | `FilterEngine.transform_text` → `apply_header_footer`; caption path `CaptionManagementEngine.format_caption` | Clear was inert (wrong metadata key); **17 owner-facing toggles were admin-gated while reading the rule unscoped** |
| 05 | Duplicate Filters | FAIL → fixed | `DurableMessagePipeline` DEDUPLICATE stage → `IMessageMapRepository.claim_content_hash` | The fingerprint was written **before** delivery and never released — undelivered content blocked later legitimate posts; check was read-then-write, not atomic |
| 06 | Replace | FAIL → fixed | `DurableMessagePipeline._apply_transforms` → `FilterEngine.replace_text`; editor `rtx:` / `rtxa:` / `rtxd:` | Applied **twice** per message (`# → ##` sent `####gold`); positional delete could remove a neighbour; entry buttons were admin-gated |
| 07 | Blacklist | FAIL → fixed | bot menu `rfl_bl:` → `ForwardRule.blacklist` → `policy_chain` → `ContentPolicy.evaluate` | The menu stored words under the pre-policy key `metadata["blacklist"]`, which no forwarding code reads: the word was saved, rendered and counted, and the message was sent anyway |
| 08 | Whitelist | FAIL → fixed | bot menu `rfl_wl:` → `ForwardRule.whitelist` → `policy_chain` → `ContentPolicy.evaluate` | Same inert storage, plus: the same key was the keyword-reply add-on's keyword list, so one list carried two unrelated meanings and neither was the allow-list the menu promises |
| 09 | Cleaner | FAIL → fixed | `ForwardRule.build_cleaner()` → `CleanerRule.clean()` in `DurableMessagePipeline._apply_transforms`; links/buttons/entities handled by the pipeline and `LinkManagementEngine` | Phones, inline buttons and empty lines were stored, rendered and counted but changed nothing; **Links/Remove (and every non-default link policy) dropped 100% of messages** (`apply_to_payload` did not exist, the `AttributeError` was swallowed by a bare `except`); the badge counted a hand-written subset |
| 10 | Filters | FAIL → fixed | `DurableMessagePipeline` FILTER stage → `FilterEngine.evaluate`; screen `rfl_lnk:` / `rfl_vid:` / `rfl_aud:` / `rfl_stk:` / `rfl_txt:` / `rfl_voc:` | The six gates enforced correctly, but the `Filters (N)` badge counted **two different wrong subsets** (video/audio/text missing; one site also counted a duplicate-filter toggle owned by another screen), the attached `FilterRule` was fetched by id with **no owner check**, and user regexes were compiled unguarded (ReDoS) |
| 11 | Set Delay | FAIL → fixed | `rdly_s:<rule_id>:<sec>` presets + typed step → `ForwardRule.delay_seconds` → `AdvancedSettingsEngine.get_dispatch_delay` → durable job claim gate (`next_retry_at`) | The delay was applied **twice** on the durable path (gate + an in-worker `asyncio.sleep`), a crafted preset stored `-600`/`inf`/`1e18` (and `nan` raised `NOT NULL constraint failed` on save), `inf` produced invalid JSON (`Infinity`) in the job payload, and the layers disagreed on the maximum (schema 86400 vs property 3600) |
| 12 | Task Status | FAIL → fixed | `ForwardRule.status_state()` → `IForwardRuleRepository.set_task_status()` (single conditional UPDATE) → the status button's `rtg:<id>:<state>` payload | The status line was rendered from `is_active` alone, so a task whose messages the pipeline was dropping and whose jobs the worker was postponing was displayed as **RUNNING 🟢**; the tap did `is_active = not is_active` — a blind flip of the row at tap time, not of what the screen showed, so a stale screen re-activated a paused task and two taps cancelled each other out; the button could not resume a paused task; `toggle()`/`toggle_owned()` exposed the same lost update to API callers |
| 13 | Duplicate Task | FAIL → fixed | Bot screen | the copy was saved through the owner-scoped *update* path, which refuses a row that does not exist yet, so every duplicate answered "📑 Rule duplicated successfully!" and created nothing. Part 2 STEER 02 found it (the alert lied while the row count stayed put); the copy now goes through `create`. |
| 14 | Rename Task | PENDING | — | — |
| 15 | Draft | PENDING | — | — |
| 16 | Advanced Settings | PENDING | — | — |
| 17 | All Settings | PENDING | — | — |
| 18 | Delete Task | PENDING | — | — |
| 19 | Route: Account | PENDING | — | — |
| 20 | Bot Token | PENDING | — | — |
| 21 | Task Categories | PENDING | — | — |
| 22 | Back to Rules | PENDING | — | — |
| 23 | Channel Comments | PENDING | — | — |
| 24 | Forward Every N | PASS | `ren:<rule_id>:<n>` + `renc:` typed step → `ForwardRule.forward_every_n` (metadata, clamped 1–1000) → `DurableMessagePipeline` stage `FORWARD_INTERVAL` / `MessageForwardingUseCase` → `IRuleCounterRepository.advance` (one UPSERT, `rule_counters`) | It was `NOT_IMPLEMENTED`: the row flipped `paid_addons["every_n"]` — a flag no engine path read — so the panel said "On" and every message still went through. Now one atomic UPSERT returns the running sequence and the slot decision is derived from it, so two workers cannot both own slot N; the counter is a row (not derived from `processed_messages`, whose 30-day sweep would silently reset cadence), albums consume one slot because the gate sits after aggregation, filtered messages never consume one because it sits after every filter, and `N = 1` writes nothing at all — no row, no read — so an untouched task is byte-for-byte the old behaviour. A crafted `ren:<id>:0` / `:1001` / `:-2` is refused, and a stranger's tap writes nothing. Evidence: `test_forward_every_n.py` (61), `test_bot_every_n_panel.py` (10) |
| 25 | Keyword Confirmation | FAIL → fixed (engine) / NOT_IMPLEMENTED (UI) (Part 2 STEER 06) | no bot-menu callback exists — the switch is the rule field only: `ForwardRule.keyword_confirmation` + `keyword_reply` (metadata, `rule_settings`) → `AdvancedSettingsEngine.classify_keyword_trigger` → `DurableQueueManager.enqueue_delivery(job_kind="keyword_reply")` → `delivery_jobs` | The reply **could not be stored**: `delivery_jobs` deduped on `UNIQUE(rule, source_chat, source_message, target)`, so the reply to a target the source was also forwarded to matched the forward's row, `INSERT OR IGNORE` dropped it, and the pipeline logged "reply enqueued" anyway. Its outcome also overwrote the forward's `message_map` row (edit/delete sync then aimed at the reply) | `job_kind` is now a column *and* part of the queue's UNIQUE key (SQLite rebuild inside a SAVEPOINT; Postgres constraint widened), the pipeline logs a refusal instead of claiming success, and only the forward writes the mapping row | `test_the_pipeline_stores_both_deliverables_for_a_keyword_match` (real pipeline → real DB → 2 rows), `test_a_keyword_reply_is_not_deduplicated_away_by_the_forward`, `test_a_reply_outcome_never_rewrites_the_forwards_mapping`, `test_a_legacy_table_is_rebuilt_with_its_rows_intact` |
| 26 | Keyword Reply Threshold | PARTIAL (Part 2 STEER 06) | same switch owner as 25 — confirmation and threshold are one path (`classify_keyword_trigger`: reply text truncated to `KEYWORD_REPLY_MAX_LEN`, add-on entitlement checked, a whitelisted keyword must match) | the store-level defect of 25 blocked every reply, so no threshold could ever be observed; the gate itself (entitlement + truncation) is enforced, but there is no per-interval counter — "threshold" currently means "the reply is only sent when a keyword matches", not a rate | same fix as 25 | same evidence as 25, plus `test_a_burst_leaves_in_source_order_for_one_target` (a burst of matched messages drains one lane in source order, so replies cannot overtake forwards) |
| 27 | Auto Delete Message | PENDING | — | — |
| 28 | Outbound Webhook | PENDING | — | — |

## 01 — Edit ON/OFF

**Switch owner:** the panel writes `ForwardRule.ignore_edits`
(`pro_bot_ui._cb_rule_toggle_edit_sync`, badge at `pro_bot_ui.py:1346`).

**Authority problem (fixed here).** Two gates decided the same thing and could
disagree. `ForwardRule.sync_edits_effective` (a legacy `metadata["sync_edits"]`
key left by older API writes and proto zero-values) reported *Sync* for a task
whose panel reads *Ignore*; the engine stayed correct only because
`SyncEngine.handle_edit` also carries its own `ignore_edits` guard. The getter
now treats the panel's switch as a veto, so the property and the engine agree
and there is a single authority. The legacy key still decides when the panel
allows edits, so clients that only know the old field keep their meaning.

**Measured behaviour — deployed `sha-649c969`, `SyncEngine.handle_edit` driven
directly with the rule the engine reloads:**

| Panel | `metadata.sync_edits` | `sync_edits_effective` (before → after) | copy updated |
|-------|------------------------|------------------------------------------|--------------|
| Sync  | absent | True → True | yes |
| Sync  | True | True → True | yes |
| Sync  | False | False → False | no |
| Ignore | absent | False → False | no |
| Ignore | True | **True → False** | no (engine guard) |
| Ignore | False | False → False | no |

Every row already matched the panel label at runtime; the corrupted value was
the reported one (row 5). One regression test fails on the pre-fix getter, which
is what makes it a regression test rather than a description.

**Also hardened:** a failed `message_map` lookup now fails *closed* (the edit is
claimed, never re-posted as a new message); an edit or caption edit retries once
inside a bounded flood-wait cap instead of silently dropping the copy; only
`SENT` mappings with a target id are updated, so a pending/failed delivery is
never "updated" as if it existed.

**Not reproduced:** the earlier claim that this switch was inert at runtime was
a probe artifact (it read the property value, not the engine's decision). The
engine guarded correctly. Corrected here rather than carried.

## 02 — Delete ON/OFF

Wired at `MessageDispatcher` (`DELETED_MESSAGES` → `handle_delete` /
`handle_delete_without_chat`), so this switch reaches the engine. Verified: on
deletes the mapped copy, off touches nothing, an unmapped id touches nothing,
and a mapping belonging to another rule's target is never deleted — delete
cannot cross tasks. A flood-wait retry is bounded and the target id is
re-asserted on the retry.

## 03 — Reply ON/OFF

**Confirmed defect:** the panel's Reply switch (`rtg_repsync:<rule.id>` →
`pro_bot_ui.py:6395` writes `metadata["sync_replies"]`, exposed in
`rule_settings.py` and read by `SyncEngine.handle_reply`) had **no production
caller** — nothing in the dispatcher or pipeline invoked `handle_reply`, so a
reply to a forwarded message was forwarded as a plain message and the thread
never survived the hop. The engine method itself was correct when called.

**Fix:** `MessageDispatcher._route_reply` runs on `NEW_MESSAGE` before album
aggregation and claims the event only when a parent mapping exists *and* the
rule's switch is on. Fails open, so an unmatched reply, a switched-off rule and
an empty body all forward exactly as before — no existing task changes
behaviour (`sync_replies` default `False`; none of the three live rules sets it).

Delivery is recorded in `message_map`, which makes a Telegram redelivery
idempotent and makes the mirrored reply itself editable/delete-syncable like any
other delivered copy. A failed reply send is logged and never blocks the
forward.

## 04 — Header NO/YES

**Where the header lives.** `ForwardRule.header` is a getter over two keys —
`custom_header` (the field the panel's edit step writes) and `metadata["header"]`
(which `to_dict`/`apply_draft` write, and which backup restore populates). The
pipeline applies it once via `FilterEngine.transform_text` →
`apply_header_footer`, and for media through
`CaptionManagementEngine.format_caption`, which shifts entity offsets past the
inserted prefix.

**Confirmed defect 1 — the clear button did not clear.** The panel's clear
callback (`pro_bot_ui._cb_smart_clear_header`) emptied `custom_header` and
`metadata["custom_header"]`, but the editor saves through the draft, which
lands in `metadata["header"]`. A rule whose header had been saved that way kept
rendering it after "delete": measured, `render()` still contained the text, and
the same value came back in the next forwarded message. Fix:
`ForwardRule.clear_header()` clears every key that can carry the header, and the
handler calls it. Five regression tests fail on the pre-fix body (revert-proof
run recorded); the old two-key clear is asserted to be inert, so a future
partial clear cannot pass silently.

**Confirmed defect 2 — the button was unreachable for its owner.** The same
handler (and 16 sibling rule toggles) opened with
`if not self._is_admin(cq.from_user.id)`, then loaded the rule with
`self._rules.get(rule_id)` — the id straight from the callback payload. So a
normal user pressing "clear header" was refused with an alert whose own text
reads *not found or not owner*, while the rule was read and written
install-wide by client-supplied id. The mandatory security rule is explicit that
a client-provided task id is not proof of ownership.

**Fix:** all 17 handlers now bind the caller's uid and go through the same
guard the other ~200 rule reads/writes use — `_owned_rule(rule_id, uid)` and
`_save_owned_rule(rule, uid)` (the draft save keeps passing
`expected_version`). An owner reaches their own task; a foreign task id returns
"not found"; the admin path (scope `None`) is unchanged. `test_header_switch_
end_to_end.py` asserts the invariant structurally — no `_cb_*` handler may call
`self._rules.update(` without both the scoped read and the scoped write — so the
next toggle added cannot reintroduce the gap.

**Recorded decision, not a defect:** `header_enabled` defaults to `False` and
no surface writes it. Gating the header on it would silently switch the header
off for every existing task, so `show_forward_header` plus the header text stay
the authorities. A test pins that decision so it changes only deliberately.

**Verified behaviour:** header and footer wrap the body once, in order;
re-applying to already-rendered text does not stack a second header (retry/regen
safe); the caption path prepends and shifts entities; Unicode and Persian RTL
headers survive byte-for-byte; an empty or whitespace header adds nothing;
replacements still run before the header and are not applied to it; one task's
header never renders for another; the round trip (SQLite) keeps the header and
the clear lands on disk.

## 05 — Duplicate Filters

**What a duplicate is here.** Two layers, both rule-scoped:

* *identity* — `skip_history` + `processed_messages(rule_id, chat_id,
  message_id)`: the same Telegram event replayed is skipped. Written only once
  the message was accepted for delivery.
* *content* — the toggle's own layer: `sha256(normalized text||caption + media
  kind [+ album id])`, keyed `UNIQUE(rule_id, content_hash)` in `content_dedup`.
  Whitespace and case are normalized, so a re-post of the same announcement
  under a new message id is a duplicate; the same text under **another task** is
  not (different rule id).

**Confirmed defect 1 — an undelivered message consumed the claim.** The
fingerprint was inserted at the DEDUPLICATE stage, which runs *before* the
filters, the keyword rules, the AI stage and the enqueue — and there were ~14
ways to return without delivering in between. A message the task's own filter
rejected, or that the queue refused under backpressure, or that threw mid-
transform, permanently marked its content as "already forwarded": the next
legitimate post of that text was dropped with no signal. The identity layer, by
contrast, was already written on delivery only (`enqueued_any`) — this was the
one that disagreed.

**Confirmed defect 2 — the check was not atomic.** `was_content_forwarded` →
`record_content_hash` is a read-then-write: two workers, or two replicas, both
read "not seen" and both forward, even though the store already had the
`UNIQUE(rule_id, content_hash)` index that could decide a winner. The steers
require an atomic claim or unique constraint where the mechanism allows it; the
constraint existed and was unused.

**Confirmed defect 3 — the dedup memory never expired.**
`purge_content_hashes(rule_id, before_ts)` existed but had **no caller
anywhere**, so `content_dedup` grew without bound and an identical post months
later was still collapsed by a task that never repeats anything.

**Fix:** `claim_content_hash` (atomic, `INSERT OR IGNORE` / `ON CONFLICT DO
NOTHING RETURNING`, decided by the unique index) replaces the read-then-write
check; the claim is recorded on the context and is only *earned* when the
message is accepted for delivery, otherwise `release_content_hash` gives it
back — from the per-rule `finally` in `process()`, so filters, keyword rules, AI
drops, backpressure and exceptions all return it; the hourly retention sweep now
purges fingerprints older than the same 30-day window as the message map.
Interface defaults keep third-party adapters working (read-then-write, no
release), and the SQL stores override them.

**Verified behaviour (17 tests, real pipeline + real SQLite store):** identical
content forwards once; toggle off forwards both; different content is not a
duplicate; whitespace/case variants are; another task keeps its own copy; the
memory survives a process restart; a filter-rejected message gives the claim
back and a later post ships; a refused enqueue does not claim; a mid-processing
exception does not poison the memory; a delivered message keeps its claim; a
dedup-store failure fails *open* (a hiccup never drops a message); eight
concurrent claims produce exactly one winner; five concurrent identical messages
produce exactly one delivery; release is scoped to its own task; retention
purges old fingerprints and keeps fresh ones.

**Recorded limitation (not a silent gap):** the legacy fallback path
(`MessageForwardingUseCase`, used only when no durable pipeline is wired — the
production wiring passes `durable_pipeline`) has no content-level dedup at all,
so the toggle does not apply there. The production path is the durable pipeline.
A terminal delivery failure after the enqueue leaves the fingerprint in place
(the claim is earned at "accepted for delivery", mirroring the identity marker);
retention bounds that, and the alternative would need a delivery-result callback
into the pipeline.

## 06 — Replace

**Expected behaviour (from the code):** a task's replacement rules are literal
needle → value pairs stored in `metadata["replacements"]`, applied in insertion
order: an exact, case-sensitive `str.replace` when the needle occurs, otherwise a
case-insensitive fallback "for handles or words"; user input is never compiled as
a pattern. The editor is `rtx:` (menu) → `rtxa:` (add, free-text step) →
`rtxd:` (delete one) / `rtxc:` (clear all).

**Confirmed defect 1 — applied twice.** `_apply_transforms` applied
`rule.replacements` at "Replacements" (before the content policy and the AI
stage) **and** again at "Final Replacements" (after the AI stage).
`str.replace` is not idempotent for an expanding rule, so a `#gold` post under
`# → ##` was sent as `####gold`; `Gold → silver` on `Gold and GOLD` sent
`silver and silver` because the second pass re-ran the case-insensitive
fallback over text the first pass had deliberately left alone. The edit path
(`FilterEngine.transform_text`, used by `SyncEngine` for edits) applied the
rules once, so a message and its later edit could disagree. Measured:
`####gold` from the real pipeline.

**Fix:** the final pass is the single authority — it runs after the AI stage (so
a rewrite cannot resurrect text the task asked to replace) and before the
header/footer (so a task rule never rewrites the task's own header). With the AI
stage off, the outgoing text is unchanged.

**Confirmed defect 2 — a positional delete could hit its neighbour.** The menu
addressed an entry by index (`rtxd:<rule>:<idx>`), so a click from a menu
rendered before another edit deleted whichever replacement had meanwhile moved
into that slot — silently, and for every subsequently forwarded message.

**Fix:** the click carries the list generation (`rule.version`, bumped by every
write) and `_resolve_replace_click` refuses a stale generation, answering with a
localized alert and re-rendering the current list instead of deleting. Out-of-range
and negative indices resolve to nothing.

**Confirmed defect 3 — the editor was admin-only and read the rule unscoped.**
`rtx:`/`rtxa:` (and 25 sibling owner-facing handlers, including the draft
editor's cancel) were behind an installation-admin gate and fetched the rule by
the id in the callback data. A task owner could not open their own replacement
rules, and the rule they addressed was read without an ownership scope. All of
them now go through `_owned_rule`/`_save_owned_rule`; the prompt openers
authorize the rule *before* stashing its id in the conversation state.

**Verified behaviour (20 tests, real pipeline + real SQLite store +
`_resolve_replace_click` driven directly):** an expanding rule is applied once
(text and caption); a plain rule once; the case-insensitive fallback does not
re-scan replaced text; three chained rules apply in insertion order in one pass;
exact match stays case-sensitive; handles match case-insensitively; an empty
needle is ignored; a regex-shaped needle is a literal (no ReDoS); Unicode/RTL
survive; one task's rules never touch another task's text; the dict survives a
persistence round trip in order; a current click resolves to its key, a stale or
out-of-range click to nothing; the new alert exists in all ten languages.

**Recorded limitations:** replacements cannot be reordered, individually
disabled, or edited in place (delete + re-add); the menu exposes delete buttons
for the first six entries only (`Clear all` is the escape hatch); a single-token
line in the add step is parsed as a deletion rule; matching is substring-based,
so a one-character needle rewrites every occurrence.

## 07 / 08 — Blacklist and Whitelist

**Expected behaviour (from the code and the settings registry):** a task's blocked
and required words live in its content policy
(`metadata["content_policy"]["blacklist_words"] / ["whitelist_words"]`,
`whitelist_mode` = `any` | `all` | `phrase`), are exposed by
`core/domain/rule_settings.py` under the `content` group, and are evaluated on the
forwarding path by `ForwardRule.policy_chain()` → `ContentPolicy.evaluate()`
(`blacklist:<word>` / `whitelist_mismatch` …). The Telegram menus
(`rfl_bl:` menu/add/clear/users, `rfl_wl:` menu/add/clear/users) are the operator
surface for the same two lists.

**Confirmed defect (both features) — the menu wrote to a key nobody reads.**
`_cb_rule_blacklist_clear` / `_step_add_bl` (and the whitelist pair) stored the
words in `metadata["blacklist"]` / `metadata["whitelist"]`, the storage the
feature used before the content policy existed. No code on the forwarding path
reads those keys: `content_policy()` folds in only `block_links` and
`remove_links`. A word added in the menu was therefore saved, rendered by
`_render_blacklist_menu`, counted in the rule screen — and never consulted.
Measured through the real pipeline against the real store: blacklist `spam` +
`buy spam now` → **delivered**; whitelist `gold` + `just chatting` →
**delivered**. The engine's own keys worked, which is what made the menu's
silence look like a working feature.

**Second confirmed defect (whitelist) — one list, two meanings.** The same
`metadata["whitelist"]` was read by `ForwardRuleService.classify_keyword_trigger`
as the *keyword-reply* keyword list, so the menu's "whitelist" was simultaneously
an unused allow-list and a live keyword list for an unrelated add-on.

**Fix — one authoritative home.** `ForwardRule.blacklist` / `.whitelist` are now
the compatibility view of the content policy: a write goes to
`blacklist_words` / `whitelist_words` and removes the legacy key, a read prefers
the policy and falls back to the legacy key, and `content_policy()` folds a
legacy list in when the policy has none. Every writer (menu, settings API,
backup restore) and the engine now read the same storage, and a task configured
before the policy existed keeps filtering instead of silently forwarding
everything it used to block. The live database was checked before the change: 3
rules, none carrying either key (nor `keyword_confirmation`), so the migration is
a compatibility path, not a data move.

**Verified behaviour (22 tests, real pipeline + real SQLite store, RED 16 failed
on the pre-fix code → GREEN):** a word added exactly as the menu writes it drops
a matching message; a message without it still forwards; the word survives a
restart; clearing in the menu stops the dropping; the menu renders what the
engine enforces; whole-word default (`spam` does not match `spammer`); matching
ignores letter case; Persian diacritic and letter variants still match; empty and
whitespace-only entries never match everything; a regex-shaped entry (`.*`,
`a{1,99999}`) is a literal, not a pattern (no ReDoS lever); one task's blacklist
never suppresses another task's message and one task's whitelist never blocks
another's; a whitelist gates both ways; `all` mode requires every word; a word in
both lists is dropped (blacklist is evaluated first); the master switch
(`content_policy_enabled = False`) turns the whole policy off; legacy rows in
either key are still enforced; a malformed policy (`blacklist_words` as a dict,
`whitelist_words` as an int, `whitelist_mode` nonsense) is coerced to empty and
crashes nothing.

**Recorded limitations:** the bot menu has no control for `whitelist_mode`, for
`regex_block` / `regex_require`, for `strip_words`, or for length/media limits —
those are reachable only through the settings surface; an *empty* whitelist
constrains nothing by design (the master switch is the safety, and a whitelist
that silently stops constraining is reported by the rule screen's count); the
menu displays the first 20 words (`ui_bl_more` reports the remainder); and
because the whitelist doubles as the keyword-reply keyword list, changing it
changes both behaviours.

## 09 — Cleaner

**Expected behaviour (from the code):** the cleaner menu (`rcl:` …) is a master
switch plus eight options — `remove_links`, `clean_usernames`, `clean_hashtags`,
`clean_phones`, `clean_buttons`, `clean_empty_lines`, `remove_emojis` and a
custom-word list. They live in the rule metadata and the engine materialises
them through `ForwardRule.build_cleaner()` into a `CleanerRule`, which
`DurableMessagePipeline._apply_transforms` runs on the body or the caption;
`CleanerRule`'s contract is removal-only — "it never drops a message".

**Confirmed defect 1 — three options were inert.** `clean_phones`,
`clean_buttons` and `clean_empty_lines` were written by the menu, rendered by the
panel and counted by the badge, but `build_cleaner()` never copied them and
`CleanerRule` had neither a field nor a branch for them. Measured on the real
pipeline before the fix: with each option alone, the outgoing text was byte-for-
byte the input.

**Confirmed defect 2 — the Links option dropped every message.**
`_apply_transforms` called `LinkManagementEngine.apply_to_payload(...)`, a method
that does not exist on the engine. The call sat inside `try: … except Exception`,
so the `AttributeError` became "cleaner failed — delivering uncleaned text" and
the message was dropped. Measured: `Deal here https://t.me/ad now` was delivered
0 times with `remove_links=True`, and 0 times for every non-default
`link_policy` (`REMOVE_ALL_URLS`, `STRIP_LINKS`, …) — the single most damaging
cleaner setting in the product silently blackholed a task's traffic.

**Confirmed defect 3 — out-of-range entities.** The cleaner is a text-only
stage, so it can delete a span an entity pointed at; the entity was still sent,
and Telegram rejects the whole message for an invalid entity range.

**Confirmed defect 4 — the badge counted its own list.** Both
`ui_rule_btn_cleaner` render sites computed the count from a hand-written subset
(neither included phones, buttons, usernames, hashtags or empty lines, and one
omitted the master switch), so a task stripping five things could show
"cleaner: 1".

**Confirmed defect 5 — a one-character entity drift.** `process_text_and_entities`
collapses whitespace and strips the ends as its last step and returned the
entity list unchanged, so an entity after the removed leading space was one
character late (measured: a bold run covering `gold` came out covering `old`).

**Fix:** `CleanerRule` gained `remove_phone_numbers`, `remove_empty_lines` and
`strip_buttons` (`build_cleaner()` passes all three through both the
`cleaner_config` and the legacy paths); `clean()` strips a contact number (a
leading "+" or a separator-bearing 10+ digit number — a bare id is left alone)
and collapses blank lines; `strip_buttons` is applied in the pipeline because it
acts on `reply_markup`, not on the text; `LinkManagementEngine.apply_to_payload()`
exists for real and works on the **live** text (so a later stage cannot resurrect
what an earlier stage removed), keeps `@` mentions, remaps entity offsets through
its own normalization, and `resolve_policy()` fails closed to `PRESERVE_ALL` for
an unknown policy instead of stripping everything; `_clamp_entities()` drops or
clips entities the text stages moved out of range; a message whose whole body is
removed is dropped with reason `empty_after_clean` and an audit record rather
than silently; and `ForwardRule.cleaner_active_option_count` is the single source
for both badge sites.

**Verified behaviour (31 tests, real `DurableMessagePipeline` + real SQLite
store):** each of the eight options observed to change the outgoing text/markup
and nothing else (`remove_links` and every non-default `link_policy` strip the
link and still deliver; buttons survive when the option is off and the message
still goes out when it is on; extra blank lines are removed only when asked); an
explicitly disabled master switch stays off and an option on without the master
switch still cleans; an unknown link policy fails closed to preserve; the drop
of an all-removed body is recorded with a reason code; entity offsets follow the
stripped text and an entity left past the end is dropped; one task's cleaner
never touches another task's message; the options survive a restart; a malformed
regex and a bombed `clean()` both leave delivery intact; captions are cleaned
like bodies; messages without text are untouched; the badge count equals the
engine's count and its master switch wins; the menu's labels exist, and are not
English placeholders, in all ten languages.

**Recorded limitations:** only "+"-prefixed or separator-bearing numbers are
stripped (a bare id is deliberately kept to avoid eating message ids and prices);
`clean_buttons` removes the whole inline keyboard rather than rewriting the
button text; the link pass runs on the cleaned text, so a policy cannot
re-introduce content the cleaner removed; and importing `pro_bot_ui` after an
event loop has been closed raises inside pyrogram (a test-hygiene constraint, not
a runtime path).

## 10 — Filters

**Expected behaviour (from the code):** the Filters screen (`rfl:` …) owns six
toggles — link, video, audio, sticker, text, voice — written by the
`rfl_lnk:`/`rfl_vid:`/`rfl_aud:`/`rfl_stk:`/`rfl_txt:`/`rfl_voc:` callbacks into
`rule.block_links`/`block_video`/`block_audio`/`block_stickers`/`block_text`/
`block_voice`, and enforced by `DurableMessagePipeline`'s FILTER stage. A task may
also attach an advanced `FilterRule` (keyword lists, media allow/deny lists,
length bounds, regex patterns; edited from the web dashboard via `filter_rule_id`)
which `FilterEngine.evaluate` applies.

**Confirmed defect 1 — the badge counted two different wrong subsets.** The
rule-list render site computed `duplicate_filters + block_links + block_voice +
block_stickers` (video, audio and text missing, and a toggle that belongs to the
duplicate-filter screen added), and the rule-detail site computed only
`filter_rule_id` — a field the bot never writes. The same task therefore showed
two different numbers, and neither matched the six toggles the screen owns.

**Confirmed defect 2 — the attached filter rule was read without an owner
check.** The pipeline fetched it with `self._filters.get_by_id(rule.filter_rule_id)`
— by id alone. The id comes from the task's own row, but nothing verified that the
row it points at belongs to the same account.

**Confirmed defect 3 — user regexes were compiled unguarded.**
`FilterEngine.evaluate` used a bare `re.compile(pattern, ...)` from the rule's
`regex_patterns`, so a nested-quantifier pattern such as `(a+)+$` evaluated
against a 4096-character body could occupy a worker for minutes. The content
policy already had a guarded compiler (`_compile_user_regex`, refusing patterns
over 1000 characters or carrying a nested quantifier) — the filter engine just
did not use it.

**Fix:** `ForwardRule.filter_active_option_count` is the single source for both
render sites and derives the count from the same six flags the engine reads (plus
one for an attached advanced rule, which the screen now also names in a localized
line, `ui_fl_advanced`, added in all ten languages); the pipeline verifies the
attached rule's owner (a rule owned by another account is ignored, with a
`[PIPELINE:FILTER_OWNERSHIP]` warning and an audit record, while a pre-ownership
row with owner 0 stays the task's own); and the guarded compiler was promoted to
`compile_user_regex` and is now shared by the content policy and `FilterEngine`,
so the guard cannot be forgotten in one place.

**Verified behaviour (38 tests, real `DurableMessagePipeline` + real SQLite
store):** with nothing set, a text and a video both go out; each of the six
toggles drops exactly the content it names and keeps the rest (link, text vs
caption-only photo, video + video note, audio, sticker, voice note); the flags
survive a restart and are re-read from the store; one task's filters never touch
another task's message; an attached rule applies its blacklist, whitelist
(requires a match), min/max length, blocked-media list and regex (matching passes,
mismatch drops); a malformed pattern and an oversized pattern are ignored rather
than applied; a `(a+)+$` pattern against a 4000-character body is refused — the
message is delivered and the measured elapsed time proves the pattern never ran; a
filter rule owned by another account is ignored; a legacy owner-0 row still
applies; the badge equals the six flags (and does **not** count the
duplicate-filter toggle); and the new label exists in all ten languages without
being an English placeholder.

**Recorded limitations:** the badge counts an attached advanced rule once,
however many conditions that rule carries (the screen names it rather than
listing its conditions); `block_voice` drops both voice notes and audio files and
`block_audio` covers both too, matching the screen's grouping; and the ReDoS
guard refuses rather than rewriting a pathological pattern, so a user who types
one gets no regex filtering from it (never a drop).

## 11 — Set Delay

**Expected behaviour (from the code):** a task can be given a forwarding delay from
the bot's Delay screen — presets (`rdly_s:<rule_id>:<sec>`) plus a typed step — which
writes `ForwardRule.delay_seconds`, and from the advanced settings' "dispatch delay"
(`forward_delay_seconds` in the task metadata). `AdvancedSettingsEngine.get_dispatch_delay`
resolves the two, and the delay must reach the real dispatch path: the durable job must
not become claimable before the delay has elapsed, must keep its task/config, and must
survive a restart.

**Confirmed defect 1 — the delay was applied twice on the durable path.** The job's
claim gate was written as `next_retry_at = now + rule.delay_seconds` while the worker,
*after* claiming, slept `min(data["advanced_delay_seconds"], 3600)`. For a rule whose
advanced delay is unset — the default, since `forward_delay_seconds` falls back to
`delay_seconds` — both numbers are the same, so a configured 300 s delay delivered after
600 s. Adding to it, that in-worker sleep dies with the process (a restart lost the rest
of the wait) and holds a worker slot and a lease for up to an hour.

**Confirmed defect 2 — the value a client sent was never validated.** The preset handler
did `rule.delay_seconds = float(sec_str)` on a callback payload, so `rdly_s:<id>:-600`
stored a negative delay that the screens displayed and the queue ignored,
`rdly_s:<id>:inf` (or `1e400`) stored `inf`, and `rdly_s:<id>:nan` was worse: SQLite
stores NaN as NULL, so the save raised `NOT NULL constraint failed:
forward_rules.delay_seconds` — a client-triggered exception in the write path. An
`inf` delay then reached the job payload, where `json.dumps` writes `Infinity`: not
valid JSON, and rejected outright by strict parsers such as Go's `encoding/json`, so
the job row became unreadable to the control plane.

**Confirmed defect 3 — the layers disagreed about the maximum.** The advanced-settings
schema advertises `0 .. 86400` s and validates that, but `forward_delay_seconds`
silently truncated to 3600 s, so a user who configured 7200 s in the advanced screen got
one hour with no indication.

**Fix:** one shared bound and one shared validator
(`MAX_FORWARD_DELAY_SECONDS = 86400`, `clamp_delay_seconds()`), applied at construction
(so every writer — bot screens, Go API → RPC, web dashboard, backup restore, duplication —
is covered, since all of them build a `ForwardRule`), in both bot handlers, and in the
advanced property, which now agrees with the schema instead of truncating. The delay is
now enforced **once, durably**: the claim gate is written from the single authority
(`get_dispatch_delay`) and the worker no longer sleeps for a delay the gate already
honoured — so a restart preserves the remaining wait instead of losing it, and no worker
slot is burned. The non-durable dispatcher path still waits inline (it has no gate) and
uses the same cap.

**Verified behaviour (35 tests, real store + real worker dispatch):** the whole
validation table (`nan`/`inf`/`-inf`/`1e400`/negative/garbage → `0.0`, `86400.5` and
`1e18` → `86400`, `"3.5"` → `3.5`); construction normalizes the first-class field and
the advanced property no longer truncates at 3600; a crafted preset payload can no
longer break the save (`nan` through the handler's own arithmetic writes `0.0`); the
typed step still reports a typo as invalid while clamping an out-of-range number; each
task keeps its own delay across a reopen; a delayed task never gates another task's
destination; the gate carries the configured delay (`now + 8s`), the advanced value wins
the gate, `0` is instantly claimable, and a **restarted** worker still refuses to deliver
early; the real dispatch of a job whose gate has elapsed performs **no sleep** for the
delay (the pre-fix code slept the full 8 s — verified by re-adding the old line and
watching this test fail); and every job payload parses under a strict JSON reader, with
no `NaN`/`Infinity` anywhere.

**Recorded limitations:** one destination is deliberately a single ordered lane, so an
earlier still-waiting job for the same target holds that target until its own delay
elapses (the later job keeps its own configuration and gate; this is ordering, not a
cross-task leak); the delay is a scheduling gate, not a calendar schedule — it starts
when the job is enqueued; and the non-durable dispatcher path still waits inline, because
it has no durable queue to hold the job.

## 12 — Task Status

**Real states (measured, not assumed).** A task is stopped by *two* independent mechanisms and the engine
obeys both. `is_active = false` is enforced by **routing**: `ForwardRule.matches_source()` answers False and
the SQL never selects the row. A pause (`is_paused` + `paused_until`) is enforced **downstream**: routing still
selects the task, the pipeline drops the message, and the durable worker postpones a job it has already
accepted (`RETRY_WAIT`) or refuses one it dequeues after the task was switched off. So the honest state is a
fold of both, and an *elapsed* `paused_until` is not a pause at all — `is_currently_paused()` has always said
so, which is exactly what the UI was not asking.

**Confirmed defect 1 — the screen showed RUNNING for a task the engine was dropping.** The status line was
`ui_rule_status_running if rule.is_active else ui_rule_status_paused`. On a paused task `is_active` is still
True, so the owner saw a green RUNNING task while every message was being dropped and every job postponed.
`status_state()` (`RUNNING` / `PAUSED` / `PAUSED until <ts>` / `STOPPED`) is now the single authority for the
status line, the button and the transition.

**Confirmed defect 2 — the tap was a flip, not a transition.** The button carried only the rule id and the
handler wrote `rule.is_active = not rule.is_active`: the new value depended on the row as it was at tap time,
not on what the user was looking at. A screen rendered while the task ran, tapped after the dashboard had
paused it, therefore re-activated it; a double tap (or two devices) cancelled itself out. The button now
carries the state it was rendered from and `set_task_status()` applies a **single conditional UPDATE** keyed on
that state, with ownership in the same statement — a stale or foreign transition writes nothing and returns
False, and the screen is re-rendered with the truth plus `ui_rule_status_stale_alert`.

**Confirmed defect 3 — the button could not resume.** Resuming only moved `is_active`, leaving `paused_until`
in the future, so the pipeline kept dropping and jobs stayed postponed. A transition to active now clears the
pause in the same statement.

**Verified behaviour (27 tests, real store + real worker):** the four states and their derivation, including
"an elapsed pause is RUNNING because the engine resumed it" and "only RUNNING means the forwarder would act";
the rendered task screen agrees for all four states; the button payload carries the rendered state; all ten
languages render and none falls back to another language's string; the current state flips and persists; two
taps on one screen produce one transition; a stale screen cannot re-activate a paused task but can resume it
from the state it now shows; an elapsed pause is not reported as a live pause; an untimed and a timed pause are
different states; a transition from another owner writes nothing; the version moves so optimistic readers see
the change; the state survives a reopen; `set_status` distinguishes `ok` / `stale` / `not found`; a paused task
is resumed *as a resume* (`is_paused` cleared, `engine_accepts_new_work()` true); a job accepted while running
is refused after the task is switched off; a job accepted before a pause is postponed, not sent; a job whose
pause has elapsed *is* delivered (the worker and the screen agree); and a resume releases the postponed rows.
With the pre-fix rendering and an unconditional write re-introduced, **4 of these fail** — they test the defect,
not the fix.

**Interface compatibility:** `set_task_status()` is concrete on the interface with a read-then-write fallback
(the same precedent as `count_by_owner`), because the backup-service fakes subclass the interface — making it
abstract broke 8 of their tests. Both production stores override it, and a guard test fails if either stops.

**Recorded limitations:** SQLite is the live store in this environment; the Postgres store mirrors the
statement and is covered by the same contract but was not executed against a live Postgres instance; a job that
a worker has already leased when the task is switched off finishes as `FAILED` (`task disabled`) rather than
being cancelled mid-`copy_message`.


## 13 — Duplicate Task (Part 2 STEER 02/03)

**Confirmed defect.** `_cb_rule_duplicate` built the copy with a fresh id and then persisted it with
`_save_owned_rule`, which is `ForwardRuleUseCases.update_owned` — that resolves ownership of an **existing**
row and raises `PermissionError("not_owner")` when the id is unknown. The copy's id is always unknown, so the
handler never reached its success alert: the raise escaped into pyrogram's dispatcher, which logged the
update and dropped it, so the user saw a tap that did nothing — no copy, no alert, no error. The dashboard route (`POST /api/rules/{id}/duplicate` → `CreateRule`) was never affected:
it clones the proto and calls create, with the owner taken from the JWT identity.

**Root cause:** a create was written as an update. **Fix:** `self._rules.create(new_rule)`.

**Evidence.** `core/tests/test_cross_tenant_boundaries.py::test_duplicating_a_task_creates_an_independent_copy_for_the_owner`
presses the real `rdup:<task>` button through the registered handler: with the pre-fix line restored it fails
with `PermissionError: not_owner` and no row; with the fix the copy exists, is owned by the duplicator, keeps the
original's routing, and deleting it leaves the original byte-identical. `…_duplicating_another_tenants_task_creates_nothing`
presses the same button as the other tenant and asserts neither a new row nor a changed victim row.

## 14 — Cross-feature interactions (Part 2 STEER 05)

Run with all switches on at once, the pipeline had three interaction defects that no single-switch test could
see. Detail, root cause and evidence are in `docs/feature-coverage-matrix.md` § "Part 2 STEER 05":

1. **Content policy applied twice** — the task's own header/footer/replacement text was re-checked against the
   task's own blacklist at the per-target gate, so a task could reject 100% of its own messages. Status:
   `FAIL → fixed`. Only `target:`-scoped overrides decide at the gate now.
2. **Cross-tenant starvation** — the single-match routing policy was global, so on a shared source channel one
   tenant's task silently consumed every message and the other tenant's task never fired. Status:
   `FAIL → fixed` (per-owner single match).
3. **Paused task logged as a routing miss** — a stopped task produced `no_matching_rules`/`RULE_NOT_MATCHED`
   and no pause-specific log line, which sends an operator hunting for a routing bug that does not exist.
   Status: `FAIL → fixed` (`rule_paused` / `RULE_PAUSED`).

Regression tests: `core/tests/test_combined_pipeline_stages.py` (13 tests, real pipeline + real SQLite).
---

## Stage 4.0 re-audit (master 35ee530, 2026-11-02)

The statuses above were evidence measured against `sha-9aeb96d` / `sha-649c969`. The
Stage 4.0 business-logic inventory re-derived every switch against `35ee530`. Results:

- **14 previously-PENDING switches resolved.** The complete grid is
  `docs/behavior-audit/feature-inventory.json` (64 features) and the per-setting
  verdicts in `docs/behavior-audit/settings-inventory.json`.
- **Confirmed defects carried forward as findings** (not fixed in the audit PR, per its
  scope rules): `dedupe_policy` inert (F-01), album TTL hard-coded (F-02), emoji regex
  Unicode damage (F-03), `remove_links`/`link_replacement` precedence (F-04), Go
  `BlockLinks` never populated (F-12), header placeholders dead on the durable path
  (F-15), `filter_inline_keyboard` unwired (F-16), bound disagreements (F-17).
  Full register: `docs/behavior-audit/FINDINGS.md`.
- **Rows 25/26 (keyword reply)** remain as previously documented; the store-level defect
  described there is fixed and the engine gate is verified reachable
  (`pipeline.py:1298-1330`).
- Row 14 (Rename), 15 (Draft), 18 (Delete Task) and the other UI-level switches were
  outside the Stage 4.0 engine scope and remain as recorded above.

