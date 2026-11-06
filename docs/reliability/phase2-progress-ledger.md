# NSE Phase 2 — Progress Ledger (CSV finding IDs → outcome)

Branch `fix/delivery-dup-and-ownership` · baseline master `fcc0dc1` (PR #340) · round 2026-11-06.
Production evidence was collected **read-only** (sqlite `.backup` snapshot + live queries); the one
production mutation is item F-01 (executed under the runbook's safeguards, verified delivered).
Raw per-record exports (server-db-audit.csv / server-db-affected-records.csv) are kept OUT of the
repository — they are production evidence, not source.

| Finding | Severity | Evidence basis | Fix | Status |
|---|---|---|---|---|
| F-01 12× `PHOTO_EXT_INVALID` FAILED (attempts=0) | P0 | snapshot + live dry-run gates 12/0/0 | conditional fail-closed requeue (runbook §4) | **EXECUTED 2026-11-06 — 12/12 delivered exactly-once, 0 WARN/ERROR** |
| F-02 3× UNKNOWN `e2e-go*` (naming: e2e-go/e2e-go2/e2e-go3) | P2 | snapshot; prior ledger pattern `e2e-go-%` matched only 1 of 3 | corrected explicit-ID DELETE (runbook §7) | PREPARED — irreversible, awaiting authorization |
| F-03 127 orphan SENT fences (no retention) | P2 | snapshot | `PurgeAgedFences` (terminal-only, UNKNOWN never swept) + hourly sweep | IMPLEMENTED + tested; production purge starts post-deploy |
| F-04 45 orphan mappings (15 in-window) | P3 | snapshot | recurrence prevented by `run_matrix` namespace cleanup (#339); residue deletion prepared | PREPARED (residue) — awaiting authorization |
| F-05 6 SENT-without-fence rows (dual write authority) | P2 | snapshot | single-owner architecture: worker enabled, Python dispatch dormant, fence ordering | VERIFIED-SOUND for current runtime; rows kept as history |
| F-07 webhook add-on enabled on 1 rule, no engine (G2) | P3 | snapshot `forward_rules.metadata` | implement-or-enforce (product decision) | TRACKED (PR #339 ledger §6.3) |
| F-08 Python reconciler repeats Go-owned UNKNOWN WARNs | P3 | live logger post-deploy delta | `_check_unknown_deliveries` stands down at `ATF_WORKER_ENABLED=1` | IMPLEMENTED + tested |
| F-09 SQLite fence writers unguarded (SENT overwritable) | P1 | Agent-B sequence (needs F-10) | `status <> 'SENT'` guards, parity with PG + Python | IMPLEMENTED + tested |
| F-10 same-worker fence claim ignores lease (double dispatch) | P1 | Agent-B exact sequence | FenceClaim pre-read state machine (sqlite+pg), FAILED re-claim preserved | IMPLEMENTED + tested (sqlite + real PG) |
| F-11 ambiguous album group copy → per-item re-upload dup; LAST-id mapping; opt-out ignored | P1 | Agent-B sequence + code read | IsAmbiguous guard, first-item mapping id, opt-out honored | IMPLEMENTED + tested |
| F-12 bot-route media refusal → permanent loss (no USER rescue) | P1 | Agent-B sequence + legacy parity | `botMediaRefused` USER fallback (opt-in aware) | IMPLEMENTED + tested |
| F-13 staged media never deleted; MediaQuotaBytes dead | P2 | grep + compose topology | `defer os.Remove` all outcomes; fake writes real files to test | IMPLEMENTED + tested |
| F-14 PG DLQ replay dead code (4-col ON CONFLICT vs 5-col key) | P2 | Agent-B + schema read | 5-col conflict target + job_kind in INSERT/lookup (both stores) | IMPLEMENTED + tested on real PG |
| F-15 reconciler strands dial-phase Unavailable | P2 | Agent-B both sequences + prod rows | `Error while dialing` marker; bare Unavailable stays UNKNOWN | IMPLEMENTED + tested |
| F-16 16 `mtx-*` SENT residue rows | P3 | snapshot | none — ages out at 7d (~Nov 11) | NO-ACTION (self-resolving) |
| F-17 timezone-dependent autoreply gate test | P3 | red on +0330 host, green on UTC CI | derive window from the account-offset clock | IMPLEMENTED + tested |
| F-19 stale stores beside live DBs (logger.db, backup-preclear) | P3 | volume listing (PR #339 §7, still true) | one-time removal | PREPARED — awaiting authorization |

## Verified-sound this round (no action)

- Preflight-before-fence ordering; `FenceAlreadySent` close-out; atomic `ReconcileUnknown` both stores;
  PG claim fails closed; split-album resume; DLQ write-once; runtime bot-token ownership checks;
  keyword-reply mapping isolation; no byte corruption in the extension chain (Agent A/B negative space).
- Post-deploy logger delta (Nov 5 20:45+): only 4 benign WARN lines — the #339 log-collapse prediction held.
- Python suite 4292 pass / 27 skip (1 environmental `is_checkout` failure: no real `.git` in export; CI green).
- Go suite + `-race` green; PG queue suite green against real PostgreSQL 17.

## Awaiting authorization (all prepared, none executed)

1. F-02 artifact deletion (corrected 3-ID list).
2. F-04 residue mapping cleanup (45 rows) — optional; aged ones are by-design retention.
3. F-19 stale-store file removal.
4. Merge/deploy of this PR (deploy also starts the F-03 retention sweep in production).
5. Phase-6 live re-upload test (docs/reliability/phase6-live-test-spec.md).
