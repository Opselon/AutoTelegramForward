# Phase 4 — Production Recovery Runbook (PREPARED, NOT EXECUTED)

**Status: dry-run validated against snapshot `/tmp/atf-phase2-snap.db` (taken 2026-11-06 22:58 local, read-only `.backup`). No production write has been performed or will be performed without explicit authorization.**

## 1. Revalidated current state (2026-11-06 snapshot)

- Exactly **12** rows match `error_detail LIKE '%PHOTO_EXT_INVALID%'` — unchanged from the historical count.
- All 12: `status='FAILED'`, `attempts=0`, `max_attempts=5`.
- Rule `40edad5a-a9b2-45f9-8eca-1f6f65619c64`: `is_active=1`, target `-1001854636317` (unchanged).
- Delivery fences (delivery_attempts) for all 12 source messages: `status='FAILED'`, `target_message_id IS NULL` → Telegram never accepted a send (server-side rejection before persist).
- message_map for all 12: `delivery_status='PENDING'`, `target_message_id IS NULL` → no delivery side effect recorded.
- Delta vs Nov 05: **none** for this group (the 10 rule-family UNKNOWN rows WERE reconciled to FAILED post-deploy — reconciler confirmed working in production; their requeue from the old ledger §5(b) remains a separate user-gated decision, out of this task's scope).

### The 12 exact job IDs

```
40edad5a-a9b2-45f9-8eca-1f6f65619c64:-1001957768371:18321:-1001854636317
40edad5a-a9b2-45f9-8eca-1f6f65619c64:-1001957768371:18323:-1001854636317
40edad5a-a9b2-45f9-8eca-1f6f65619c64:-1001957768371:18326:-1001854636317
40edad5a-a9b2-45f9-8eca-1f6f65619c64:-1001957768371:18332:-1001854636317
40edad5a-a9b2-45f9-8eca-1f6f65619c64:-1001957768371:18336:-1001854636317
40edad5a-a9b2-45f9-8eca-1f6f65619c64:-1001957768371:18340:-1001854636317
40edad5a-a9b2-45f9-8eca-1f6f65619c64:-1001957768371:18347:-1001854636317
40edad5a-a9b2-45f9-8eca-1f6f65619c64:-1001957768371:18355:-1001854636317
40edad5a-a9b2-45f9-8eca-1f6f65619c64:-1001957768371:18361:-1001854636317
40edad5a-a9b2-45f9-8eca-1f6f65619c64:-1001957768371:18362:-1001854636317
40edad5a-a9b2-45f9-8eca-1f6f65619c64:-1001957768371:18366:-1001854636317
40edad5a-a9b2-45f9-8eca-1f6f65619c64:-1001957768371:18375:-1001854636317
```

(msg ids 18321–18375, created Nov 05 09:18–14:35, source `-1001957768371` → target `-1001854636317`)

## 2. Safety properties (why requeue is duplicate-safe)

Each job's whole failure history is `400 PHOTO_EXT_INVALID` from `messages.SendMedia` — a **server-side rejection before persist**: Telegram never accepted any upload for these. Fences confirm (`target_message_id` NULL), mappings confirm (NULL/PENDING), `attempts=0`. The fixed pipeline (worker `fcc0dc1`: stagedExtension + target-side-rejection fallback) will stage with the real extension and deliver exactly once.

## 3. Dry-run mode (run first — makes zero writes)

```sql
-- DRY-RUN A: expected exactly 12 rows, all preconditions true
SELECT j.id, j.status, j.attempts,
       (SELECT a.target_message_id FROM delivery_attempts a
         WHERE a.source_chat_id = j.source_chat_id
           AND a.source_message_id = j.source_message_id
           AND a.target_chat_id    = j.target_chat_id) AS fence_tmsg,
       (SELECT m.target_message_id FROM message_map m
         WHERE m.source_chat_id = j.source_chat_id
           AND m.source_message_id = j.source_message_id
           AND m.target_chat_id    = j.target_chat_id) AS map_tmsg
FROM delivery_jobs j
WHERE j.error_detail LIKE '%PHOTO_EXT_INVALID%'
  AND j.status = 'FAILED' AND j.attempts = 0;

-- DRY-RUN B (fail-closed gate): must return exactly 12 and 12
SELECT
  (SELECT COUNT(*) FROM delivery_jobs
    WHERE error_detail LIKE '%PHOTO_EXT_INVALID%'
      AND status='FAILED' AND attempts=0)                                   AS eligible,
  (SELECT COUNT(*) FROM delivery_jobs
    WHERE error_detail LIKE '%PHOTO_EXT_INVALID%'
      AND status='FAILED' AND attempts=0
      AND EXISTS (SELECT 1 FROM delivery_attempts a
                  WHERE a.source_chat_id=delivery_jobs.source_chat_id
                    AND a.source_message_id=delivery_jobs.source_message_id
                    AND a.target_chat_id=delivery_jobs.target_chat_id
                    AND a.target_message_id IS NOT NULL))                   AS fence_blockers,
  (SELECT COUNT(*) FROM delivery_jobs
    WHERE error_detail LIKE '%PHOTO_EXT_INVALID%'
      AND status='FAILED' AND attempts=0
      AND EXISTS (SELECT 1 FROM message_map m
                  WHERE m.source_chat_id=delivery_jobs.source_chat_id
                    AND m.source_message_id=delivery_jobs.source_message_id
                    AND m.target_chat_id=delivery_jobs.target_chat_id
                    AND m.target_message_id IS NOT NULL))                   AS map_blockers;
-- ABORT unless eligible=12, fence_blockers=0, map_blockers=0
```

Snapshot result of dry-run: **eligible=12, fence_blockers=0, map_blockers=0** ✅

## 4. Apply mode (CONDITIONAL — every row guarded individually; REQUIRES AUTHORIZATION)

```sql
BEGIN IMMEDIATE;

CREATE TEMP TABLE _recovery_rows AS
  SELECT id, status, error_detail, updated_at FROM delivery_jobs
  WHERE error_detail LIKE '%PHOTO_EXT_INVALID%'
    AND status='FAILED' AND attempts=0
    AND NOT EXISTS (SELECT 1 FROM delivery_attempts a
                    WHERE a.source_chat_id=delivery_jobs.source_chat_id
                      AND a.source_message_id=delivery_jobs.source_message_id
                      AND a.target_chat_id=delivery_jobs.target_chat_id
                      AND a.target_message_id IS NOT NULL)
    AND NOT EXISTS (SELECT 1 FROM message_map m
                    WHERE m.source_chat_id=delivery_jobs.source_chat_id
                      AND m.source_message_id=delivery_jobs.source_message_id
                      AND m.target_chat_id=delivery_jobs.target_chat_id
                      AND m.target_message_id IS NOT NULL);

-- fail-closed: exactly 12 or roll everything back
SELECT CASE WHEN (SELECT COUNT(*) FROM _recovery_rows) = 12
            THEN 'OK-PROCEED' ELSE (SELECT RAISE(ABORT, 'precondition count mismatch')) END;

UPDATE delivery_jobs
SET status='PENDING', next_retry_at=0, lease_until=0, worker_id='',
    error_detail = error_detail || ' | requeued-by:phase2-runbook-2026-11-06',
    updated_at=strftime('%s','now')
WHERE id IN (SELECT id FROM _recovery_rows)
  AND status='FAILED' AND attempts=0;   -- re-check under the transaction

SELECT changes();  -- must be 12, otherwise ROLLBACK
COMMIT;
```

Notes:
- `error_detail` is **appended**, not blanked — exhausted-operation evidence preserved (no evidence destruction).
- The UPDATE predicate re-checks status/attempts under the write lock → no race with the worker.
- Rows re-enter the normal claim path; the fixed worker applies the extension staging path.

## 5. Post-apply verification

```sql
SELECT status, COUNT(*) FROM delivery_jobs
 WHERE id IN (SELECT id FROM _recovery_rows) GROUP BY status;   -- expect 12 PENDING (then SENT as worker drains)
-- later: no PHOTO_EXT_INVALID re-failures, no duplicate message_map rows:
SELECT COUNT(*) FROM message_map
 WHERE source_chat_id='-1001957768371'
   AND source_message_id IN (18321,18323,18326,18332,18336,18340,18347,18355,18361,18362,18366,18375)
   AND target_message_id IS NOT NULL;                            -- expect ≤ 12, one per source
```

## 6. Rollback

Full-file rollback: `cp /tmp/atf-phase2-snap.db`-style pre-apply snapshot (take a fresh `.backup` immediately before apply). Row-level rollback:

```sql
UPDATE delivery_jobs SET status='FAILED', next_retry_at=0, lease_until=0, updated_at=strftime('%s','now')
WHERE id IN (SELECT id FROM _recovery_rows)
  AND status='PENDING' AND error_detail LIKE '%requeued-by:phase2-runbook-2026-11-06%';
```

(The original `error_detail` text is preserved by the append-only requeue marker.)

## 7. Appendix A — corrected artifact-removal SQL (Phase 5, DO NOT RUN — no authorization)

**CRITICAL CORRECTION vs the PR #339 ledger §5(c):** the 3 artifacts are `e2e-go-1793747456`, `e2e-go2-1793747663`, `e2e-go3-1793747953`. The old pattern `id LIKE 'e2e-go-%'` matches **only 1 of 3** (`e2e-go2-…`/`e2e-go3-…` fail the literal `-` after `e2e-go`). Corrected form (explicit ID list — no wildcards):

```sql
DELETE FROM delivery_jobs WHERE id IN (
  'e2e-go-1793747456','e2e-go2-1793747663','e2e-go3-1793747953'
) AND status='UNKNOWN';   -- expect changes()=3
```

Fence rows for source messages 999997-999999 are intentionally **not** touched (fence modification is outside authorization); they age out of the reconciler's in-window orphan checks and are already outside the mapping-retention asymmetry (no message_map rows exist for them).

## 8. Appendix B — observed (not actionable here)

16 `mtx-*` SENT rows in production DB, newest 2026-11-04 19:26 — live-fire matrix residue predating the run_matrix namespace guard (6274c96). All SENT, no failure state; report-only.
