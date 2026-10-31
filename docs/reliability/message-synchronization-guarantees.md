# Message Synchronization Guarantees

**Scope:** edit, caption and delete propagation for already-delivered copies.
**Builds on:** PR #274 (delivery pipeline), PR #275 (E2E-3/4 fixes), PR #276
(reconciliation). Evidence for every claim is either a reproduction in
`core/tests/` or the code paths cited below.

The forwarding pipeline delivers a message once. Synchronization is everything
that happens *after* that: the source's owner edits, recaptions or deletes the
original, and the destination copies must follow. This document states exactly
what the system does and does not guarantee here — because a synchronization
gap is the most user-visible form of "partial forwarding" that is not a
delivery failure at all.

## The guarantee, stated plainly

**At-least-once, best-effort eventual synchronization for edits and deletes of
messages that were successfully delivered.** Specifically:

* an edit of a delivered message is applied in place to every mapped target
  copy, for every active rule with edit sync enabled;
* a delete of a delivered message removes every mapped target copy, for every
  active rule with delete sync enabled;
* an edit arriving **before** delivery completes is folded into the pending
  delivery job so the first post carries the new text (E2E-3, PR #275);
* a transient sync failure is retried a bounded number of times with backoff
  (E2E-4, PR #275);
* a permanent sync failure is logged with the identifiers an operator needs
  and is **never** converted into a duplicate re-post.

This is **not** exactly-once synchronization. Two copies of an edit can land
if Telegram redelivers the update and the first application's success was not
yet visible to the second — the same at-least-once contract the delivery
pipeline carries. The `message_map` is what keeps that bounded: the dispatcher
claims an edit only when a mapping exists, so an unmapped message is a first
delivery, never a duplicate of a live post.

## What is synchronized

| event | synchronized? | mechanism |
|---|---|---|
| text edit of a delivered copy | yes | `SyncEngine.handle_edit` → `edit_message_text` per mapped target |
| caption edit of a delivered media copy | yes | `handle_edit` with `has_media` → `edit_message_caption` |
| edit while delivery is still pending | yes (PR #275, E2E-3) | `fold_edit` splices the text into the pending job's payload |
| delete of a delivered copy | yes | `handle_delete` → `delete_messages` per mapped target |
| delete without a chat in the update | yes (unambiguous ids only) | `handle_delete_without_chat` resolves the source chat from the map, skipping ambiguous ids |
| album / media-group edit | per-item only | each item has its own mapping; there is no grouped edit |
| poll vote / poll close | **no** | not implemented — the poll itself cannot be copied (verified against live Telegram, see the capability matrix) |
| pin / unpin | **no** | not implemented |
| reaction | **no** | not implemented |
| view counter / forwards counter | **no** | not implemented (and Telegram does not propagate them on copies) |

## What is deliberately not retried forever

A sync failure is classified, not just counted:

* **`MessageNotModified`** — treated as success. The target already has this
  text; the edit is idempotent.
* **`MessageIdInvalid`** — the target copy was deleted first. Nothing to do;
  the correct state already holds.
* **Permission errors (`ChatAdminRequired`)** — permanent. Retrying burns time
  before the same failure, so the sync stops and logs.
* **Transient errors (network, FloodWait inside the cap)** — retried up to
  three attempts with 1s–3s backoff and jitter (E2E-4).

The reconciliation layer (PR #276) reports any delivery that ended with no
observable outcome — including a mapping whose job vanished — so a sync gap
that would otherwise sit silently does get noticed.

## Known limitations

1. **No cross-process sync queue.** Edit/delete propagation is synchronous in
   the event handler, with bounded in-call retry. A crash mid-sync leaves the
   copy stale until the next edit or an operator notices via reconciliation.
   Deliberate: the alternative is a second durable queue whose invariants would
   drift from the delivery queue's, and the mission's own storage rules
   discourage it.
2. **Album edits are per-item.** Editing the album's *caption* in Telegram
   applies to the group; the forwarder maps each item individually and edits
   each copy's own caption. A grouped edit that changes ordering or membership
   is not modelled.
3. **Ambiguous deletes are skipped, not guessed.** `updateDeleteMessages`
   (private chats, basic groups) carries ids without a chat. The map resolves
   the chat only when the id maps to exactly one source; two monitored chats
   can hold the same id, and removing the wrong copy is worse than missing a
   delete.
4. **Sync is skipped for disabled rules.** An edit arriving after the owner
   disabled the task does not propagate — the task is no longer making
   promises about its copies. The copies remain until the owner deletes them
   or the rule's delete-on-disable setting does.
5. **Service messages are never synchronized.** They are filtered at ingress;
   there is nothing to synchronize.

## Regression tests

| file | covers |
|---|---|
| `core/tests/test_edit_sync_recovery.py` (PR #275) | E2E-3 pending-edit folding, E2E-4 transient retry, permanent-failure stop, FloodWait retry, idempotent `MessageNotModified`, fails-closed probe |
| `core/tests/test_sync_switches_end_to_end.py` | the panel's edit/delete switches deciding runtime behaviour, redelivery not duplicating a reply, deletes not crossing to another rule's target |
| `core/tests/test_delivery_reconciliation.py` (PR #276) | orphan mappings — the state mismatch that a sync gap can leave behind |
