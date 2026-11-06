# Phase 5 — e2e-go Artifact Cleanup Manifest (INVESTIGATION ONLY — NOTHING DELETED)

Snapshot basis: `/tmp/atf-phase2-snap.db` (2026-11-06 22:58) + live worker container inspection.

**Naming correction (affects the old recovery SQL):** the historical label "3 × `e2e-go-*`" hides that the actual IDs use three different prefixes — `e2e-go-`, `e2e-go2-`, `e2e-go3-`. The prepared SQL in `docs/reliability/pr339-audit-ledger.md` §5(c) (`id LIKE 'e2e-go-%'`) would remove only artifact #1. See runbook Appendix A for the corrected explicit-ID statement.

## Per-artifact manifest

| # | Job ID | Created | Error class | rule_id | Src msg | Fence (delivery_attempts) | message_map | Physical staged file (SHA-256) | diagnostic_events |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `e2e-go-1793747456` | 2026-11-03 23:10:56 | `Unavailable: dial tcp …` (gRPC conn refused) | `ea565d5c-…` (test rule) | 999999 | status=UNKNOWN, target_message_id=NULL, attempts=0, worker `a168ab7e95e1-1` | none | **absent** — `/tmp/atf-media` does not exist in atf-worker | 0 rows |
| 2 | `e2e-go2-1793747663` | 2026-11-03 23:14:23 | same | same | 999998 | same pattern | none | **absent** | 0 rows |
| 3 | `e2e-go3-1793747953` | 2026-11-03 23:19:13 | same | same | 999997 | same pattern | none | **absent** | 0 rows |

## Assessment

1. **Provably no Telegram side effect:** the RPC dial never connected (`Unavailable`, transport-level, before any handler). `attempts=0`, fence `target_message_id=NULL`, zero message_map rows.
2. **No physical artifact exists:** the media staging directory is absent in the worker container; there is nothing to hash and nothing retaining bytes. SHA-256: n/a (artifact is the DB row itself).
3. **No referencing records:** DLQ empty; diagnostic_events 0 rows for all three (job-id and source-message lookups); only the fences reference the synthetic source messages 999997–999999.
4. **Forensic value:** none beyond the row itself — the failure class is fully recorded in `error_detail`, and the run_matrix seeding path that produced them was already fixed (6274c96 namespace guard refuses non-`mtx-*` ids).
5. **Rule context:** parent rule `ea565d5c-…` is the live-fire test rule (52 SENT + 4 FEN-skip FAILED + these 3 UNKNOWN); it remains active production data — out of scope.

## Recommendation per artifact

| Artifact | Recommendation |
|---|---|
| `e2e-go-1793747456` | **Safe to remove after authorization** (corrected SQL, runbook Appendix A) |
| `e2e-go2-1793747663` | **Safe to remove after authorization** |
| `e2e-go3-1793747953` | **Safe to remove after authorization** |

No deletion performed. Fence rows for 999997–999999 are left untouched (fence modification not authorized; they age out of in-window orphan checks naturally). Related-but-out-of-scope: 16 `mtx-*` SENT rows are separate live-fire residue — report-only.
