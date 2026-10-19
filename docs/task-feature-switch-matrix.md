# Per-task feature switch matrix

Statuses are **evidence-based**: a switch is `PASS` only when the forwarding
engine's real behaviour matches the label — never because the screen renders, a
database value exists, or unit tests are green. Evidence quoted below was
measured against a running stack (`sha-649c969`, 5/5 `atf-*` containers healthy)
and/or by driving the real engine through its real entry point.

Legend: `PASS` behaviour verified · `FAIL` confirmed defect · `PARTIAL` some
paths unverified · `NOT_IMPLEMENTED` no engine support · `PENDING` not yet audited.

| # | Switch | Status | Engine entry point | Evidence |
|---|--------|--------|--------------------|----------|
| 01 | Edit ON/OFF | PASS (was PARTIAL) | `SyncEngine.handle_edit` via `MessageDispatcher._route_edit` | Six-row truth table on the deployed image; see below |
| 02 | Delete ON/OFF | PASS | `SyncEngine.handle_delete` / `handle_delete_without_chat` via `MessageDispatcher` (DELETED_MESSAGES) | On/off, unmapped, cross-rule isolation, flood retry — `test_sync_switches_end_to_end.py` |
| 03 | Reply ON/OFF | FAIL → fixed | `SyncEngine.handle_reply` via `MessageDispatcher._route_reply` (**new**) | The switch had **no production caller**; a reply was never mirrored |
| 04 | Header NO/YES | FAIL → fixed | `FilterEngine.transform_text` → `apply_header_footer`; caption path `CaptionManagementEngine.format_caption` | Clear was inert (wrong metadata key); **17 owner-facing toggles were admin-gated while reading the rule unscoped** |
| 05 | Duplicate Filters | PENDING | — | — |
| 06 | Replace | PENDING | — | — |
| 07 | Blacklist | PENDING | — | — |
| 08 | Whitelist | PENDING | — | — |
| 09 | Cleaner | PENDING | — | — |
| 10 | Filters | PENDING | — | — |
| 11 | Set Delay | PENDING | — | — |
| 12 | Task Status | PENDING | — | — |
| 13 | Duplicate Task | PENDING | — | — |
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
| 24 | Forward Every N | PENDING | — | — |
| 25 | Keyword Confirmation | PENDING | — | — |
| 26 | Keyword Reply Threshold | PENDING | — | — |
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
