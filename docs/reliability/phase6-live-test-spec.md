# Phase 6 — Controlled Telegram Live-Test Specification (PREPARED — NOT EXECUTED)

**No real Telegram message is sent, forwarded, copied or re-uploaded by this task.** Execution requires separate explicit authorization. This document is the exact execution procedure for that future authorized run.

## 0. Objective

Prove the end-to-end `PHOTO_EXT_INVALID` recovery path against real Telegram: a media message whose copy is rejected by the target is staged with its real extension, re-uploaded via `messages.SendMedia`, delivered exactly once, and recorded correctly (fence → mapping → job).

## 1. Test assets & prerequisites

| Item | Requirement |
|---|---|
| Worker session | The production worker's USER session (`sessions` table, healthy, not flood-limited). Verify `GET /healthz` → `db_connected=true`, session ready. |
| Source | A dedicated test channel owned by the operator (NOT `-1001957768371`; never a private user chat). Post the probe media here. |
| Target | A second dedicated test channel where the worker's account is admin (NOT `-1001854636317`). |
| Rule | Create rule `id=<test-rule-uuid>` source→target, `delivery_transport=USER` (default), **`fallback_enabled=true`**, no filters, no FEN, `content_mode=AUTO`, `album_mode` fixed per cell. |
| Isolation | Use the live-fire matrix namespace discipline (`worker/test/live/run_matrix.py` clone/drop helpers, `mtx-` prefix) or an equivalent dedicated rule id — production jobs must remain untouched. |
| Version gate | Worker must report `commit_sha` of the PR head that ships this phase's fixes (verify `docker logs atf-worker \| jq -r .commit_sha`). |

## 2. Probe media

| Cell | Media type | Original filename | Expected runtime `GetFileExtension` | Expected staged name |
|---|---|---|---|---|
| L1 | photo (JPEG) | n/a (MTProto has none) | `.jpg` (adapter photo branch) | `<jobID>.jpg` → `FileName job-<id>-media.jpg` |
| L2 | document (real `.Mp4` uppercase name) | `clip.Mp4` | `.Mp4` (raw suffix passthrough) | normalized `.mp4` |
| L3 | document, nameless | (no file_name) | `""` | bare `<jobID>` → `send_document` (extension-less is correct for documents) |
| L4 | video note | n/a | `""` → kind fallback | `.mp4` → `send_video` |
| L5 | album ×3 photos (album_mode=album) | n/a | group copy path | group `CopyMediaGroup`; no per-item re-upload on success |

All probe assets are uploaded fresh by the operator into the source channel; no production content, no private messages.

## 3. Expected RPC sequence per cell (L1 canonical)

1. Ingestion → enqueue (`delivery_jobs` PENDING, `message_map` PENDING).
2. Worker claim → `FenceClaim` (delivery_attempts CLAIMED).
3. `CopyMessage` → Telegram returns `400 PHOTO_EXT_INVALID` → classified `IsMediaRejectedByTarget` → `NeedsReupload()=true`.
4. `fallback_enabled=true` → `reuploadMedia`: `GetFileExtension` → `.jpg` → `DownloadMedia` to `/tmp/atf-media/<jobID>.jpg` → `PrepareMediaUpload` (token == staged path) → `SendMedia` → **Telegram accepts**, returns Message id.
5. Persist: `FenceRecordSent(tmsg)` → `NoteMapping` → `MarkSent` → job SENT, mapping SENT with target id.
6. Staged file `/tmp/atf-media/<jobID>.jpg` **removed** (this phase's cleanup fix — assert absent).

## 4. Expected database transitions (per cell)

- `delivery_jobs`: exactly one row, final `SENT`, `attempts=1`, `error_detail` empty (or unchanged), `worker_id` set.
- `delivery_attempts`: one row `SENT` with `target_message_id` = accepted id; no second row.
- `message_map`: one row `SENT`, `target_message_id` non-null, single row per source (album: one row keyed by media_group_id).
- No `dead_letter_queue` rows; no `UNKNOWN` states at any point; no diagnostic ERROR events.

## 5. Log / correlation expectations

- One `correlation_id` spanning copy-failure → re-upload → sent.
- WARN (single) for the copy rejection with `error_type=PHOTO_EXT_INVALID`; no stack noise; no media bytes in logs; no source/target titles leaked beyond ids already used by the existing redaction rules.

## 6. Flood-wait & retry safety

- If Telegram returns FLOOD_WAIT at any step, the run **aborts** (do not sleep-loop a manual test): record `retry_after`, wait ≥ that value + 60s manually, re-run the cell from a fresh probe message.
- Max 1 execution per cell per 10 minutes; abort the whole run on 2 consecutive FLOOD_WAITs.

## 7. Abort conditions (stop immediately, no state cleanup by hand)

- Any job lands `UNKNOWN` (ambiguous outcome) — preserve as forensic evidence, resolve only through the reconciler procedure.
- Any job lands `FAILED` with an error other than the deliberately provoked one.
- Any write appears in production rules/queues outside the test namespace (`mtx-`/test-rule id check before AND after).
- Duplicate target message observed for a cell.

## 8. Cleanup requirements

- Drop the test rule (run_matrix `drop_rule` pattern), delete probe messages from source+target channels, allow job rows to age out under the 7-day retention (do not hand-delete), remove `/tmp/atf-media` leftovers only if the cleanup fix is verified absent (which would itself be a finding).

## 9. Exact commands (authorized run only)

```bash
# preflight
ssh vps 'docker logs atf-worker --tail 1 | jq -r .commit_sha'          # must equal PR head
ssh vps 'curl -s localhost:<worker-healthz>/healthz'                   # db_connected true

# per cell: post probe media in source channel, then
sqlite3 /var/lib/docker/volumes/autotelegramforward_atf-data/_data/atf.db \
  "SELECT id,status,attempts,error_detail FROM delivery_jobs WHERE rule_id='<test-rule-uuid>';"
# expect: SENT ×1 per cell, then run the §4 verification queries verbatim
```

## 10. Success criteria (all required)

1. Copy rejection observed (WARN with PHOTO_EXT_INVALID) — recovery path actually exercised, not bypassed.
2. Staged filename carried the expected extension (worker log line with staged path).
3. Telegram accepted exactly one send per probe; target shows exactly one message per probe.
4. Fence/mapping/job all SENT with the same target id (§4).
5. Staged file removed post-send.
6. Zero unrelated rows touched (before/after row-count delta limited to the test namespace).
7. No sensitive content in logs (manual grep of the correlation_id lines).
