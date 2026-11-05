# PR #339 Engineering Audit Ledger — Task Engine & Worker Reliability Round

Branch `fix/media-rejection-reupload` · baseline master `36b7529` (PR #338) · audit window 2026-11-05.
Everything below is evidence-backed; production findings were collected **read-only** against `vps-root`.

## 1. Issue ledger

| # | Issue | Component | Severity | Root cause | Fix commit | Regression test | Status |
|---|---|---|---|---|---|---|---|
| W1 | No reconciliation loop — `FenceResolveUnknown` had zero callers; 13 production UNKNOWN jobs stranded 20+ h (10 since Nov 4, all `gRPC Unimplemented` from the pre-#330 missing-SendMedia era) | worker queue/engine | P1 | The fence documented "reconciliation decides" but the decision step never existed | `ffbef52` | `queue/reconcile_store_test.go` (5), `engine/reconciler/reconciler_test.go` (4) | FIXED (deploy-gated) |
| W2 | DLQ replay of a purged row rebuilt a forward job **without** its `message_map` placeholder → worker's UPDATE-only `NoteMapping` silently drops the confirmed target id; edit/delete sync goes blind | core persistence (sqlite+pg) | P2 | Mapping retention (30 d) outlives nothing — DLQ entries persist while mappings purge; rebuild path forgot the placeholder | `d535a18` | `test_dead_letter_replay.py` (3 new) | FIXED |
| W3 | PHOTO_EXT_INVALID root cause: re-upload staged files as `job-<id>-media` with **no extension** → Telegram `messages.SendMedia` rejects (12 prod jobs died permanently) | worker delivery | P1 | `GetFileExtension` RPC fully implemented end-to-end but never wired | `fea2665` | `delivery_test.go` (7 new) + `telegram_runtime/tests/test_file_extension.py` (8) | FIXED (deploy-gated) |
| W4 | Target-side media rejection (`PHOTO_EXT_INVALID`) never triggered the re-upload fallback (only `FORWARDING_RESTRICTED` did) | worker delivery | P2 | Fallback condition checked one error class | `d5f81e1` | `TestMediaForwardTargetRejectsCopy`, `TestMediaRejectedNoFallback` | FIXED |
| G1 | 3 paid add-ons unreachable from every production client: UI records `kw_reply`/`auto_del`/`affiliate`; engines gate on `keyword_reply`/`auto_delete_message`/`affiliate_kit` | bot/web ↔ engines | P1 | Key vocabulary drift between catalogue and engine; canonical keys existed only in tests | `d535a18` | `worker addon_alias_test.go` (9 cases) + `test_phase2_advanced_settings_execution.py::TestCatalogueAddonKeyAliases` (5) | FIXED |
| F2 | Deliberate non-deliveries (filter drop, block_links, FEN cadence skip) counted as `failed`; filter drop also failed the filter stage → healthy task reads degraded | worker ops | P3 | Rejections had no outcome of their own | `d535a18` | updated FEN/link-block tests + ORD-002 accepts `filter:skipped` | FIXED |
| F3 | `worker/test/live/run_matrix.py` runs against the **production** DB and kept its replica's job rows (`keep_jobs=True`) — the seeding path behind orphaned `e2e-go-*`/`mtx-*` rows | live-fire tooling | P2 | Cleanup stopped at rules+counters; no replica-namespace guard | `6274c96` | (tooling; guard refuses non-`mtx-*` ids) | FIXED |
| F1 | `ingestion_gaps` unbounded growth — 468 terminal rows, no retention | core persistence | P3 | Terminal rows are loss records with no sweep | `6274c96` | `test_terminal_gaps_age_out_but_open_ones_never_do` | FIXED |
| O1 | `db_connected=false`/degraded on a healthy unpolled worker (DB probe piggybacked on `/healthz`) | worker health | P2 | Probe ran only when someone polled | `f0426a1` | (build; verified on vps suite) | FIXED |
| O2 | No redaction on the stdout JSON sink (repo rule: `[REDACTED]`) | worker logging | P2 | Redaction lived only in the central client | `f0426a1` | (build; shared rule set by construction) | FIXED |
| O3 | E2E latency ring blind to every failure/defer path (contradicting `Process`'s own comment) | worker delivery | P3 | `handleFailure` never called `e.outcome` | `f0426a1` | existing failure-path tests still pass | FIXED |
| O4 | `worker_id` absent from all delivery log lines; failure→ring lost `correlation_id`; AI stage counted non-AI jobs and double-counted real ones; `ai` force-enabled without a provider | worker observability | P3 | field sets / counters drifted from the logging contract | `f0426a1` | (build; verified) | FIXED |

## 2. Feature verification matrix (18 add-ons + core settings)

Full matrix with file:line evidence was compiled during this round (feature-execution audit). Summary:

**Verified executed by the Go Worker** (execution site + tests): Forward Every N, Keyword Reply engine, Link Buttons, Watermark, Cleaner Protocol, Smart AI Rewrite, Duplicate-prevention *fence* (send idempotency), Whitelist/Blacklist, Replacements, Headers/footers, Topics, Source/target routing + loop preflight, `fallback_enabled`, **BOT transport** (stage-3 ledger concern resolved — BotSendText/BotCopyMessage/auto-delete/branding all implemented), Branding hop, Auto-Reply/PVA engine, Auto Delete scheduling (`scheduleAutoDelete` + scheduler).

**Repaired this round:** Keyword Reply / Auto Delete / Affiliate Kit entitlement reachability (G1).

**Genuinely not implemented (flags persist, no engine consumer)** — the catalogue rows are decorative for: Channel Comments, Outbound Webhooks, Crypto Mode, OCR Image AI, Auto Post Scheduler, Power On/Off Schedule, Process Raw Text, Button To Text (plus hidden extras: News Digest, Translate, crypto sub-rows). Two of them (`process_raw_text`, `button_to_text`) are even baked into job payloads (`durable_queue.py:391-392`) that nothing reads.

**Required follow-up (tracked, not silently ignored):** either implement these engines or enforce their unavailability honestly in the catalogue UI (10-locale copy work). Duplicate Filters is **Python-ingest-only** since the event-dedup gate was deliberately removed from the worker (`delivery.go:474-495`, after it dropped 100 % of a live task's traffic) — worker-owned delivery has no content dedup today. Single-engine features (`processing delay`, `sync_replies`, `sync_edits/sync_deletes`) execute only in Python; the strangler-fig handover has not reached them.

## 3. Production DB diagnostics (read-only, `atf.db` snapshot)

| Finding | Evidence | Disposition |
|---|---|---|
| 12 jobs FAILED, `attempts=0`, `PHOTO_EXT_INVALID (caused by messages.SendMedia)` | rule `40edad5a`, source `-1001957768371` → target `-1001854636317`, msg ids 18321–18375, Nov 05 09:18–14:35 | Root cause W3/W4 (both fixed). No delivery side effect (fence `tmsg=None`, `message_map.target_message_id=NULL`). Recovery = requeue; see §5 |
| 10 UNKNOWN jobs (rule family) + 3 UNKNOWN `e2e-go-*` test artifacts | `Uncertain outcome: rpc error: code = Unimplemented desc = Unexpected <class 'NotImplementedError'>` — server rejected the RPC **before any handler ran** | Provably no Telegram side effect. The new reconciler will auto-close the 10 to FAILED post-deploy; the 3 test artifacts are deletable (approval-gated) |
| 7 other FAILED jobs | 1 × `CHAT_ADMIN_REQUIRED` (genuine permission gap — needs the owner to grant admin), 2 × filter rejections, 4 × FEN cadence skips | Correct terminal states (rejections now counted as `rejected`, not `failed`) |
| DLQ empty; no synthetic leftovers; cursor at 18375 | — | Queue healthy; worker idle |
| WAL 7.5 MB, checkpoint lag | — | Monitor; not actionable at this scale |

## 4. CI history (this branch)

| Commit | CI | CodeQL | Notes |
|---|---|---|---|
| `d5f81e1` (fallback classification) | green | green | — |
| `fea2665` (staging extension) | green | green | — |
| `ffbef52` (reconciler) | green | green | — |
| `d535a18` (replay/alias/reject) | **failure** → repaired | green | ORD-002 pinned the old filter-stage semantics (updated to accept policy-skip); 2 new Python tests used the wrong repo class (fixed) |
| `f0426a1` (observability) | running | green | — |
| `6274c96` (retention + matrix cleanup) | running | pending | — |

CI-log deep inspection findings (all 15 jobs read): race detector confirmed wired in `go-worker-tests`; 0 Go skips; pytest 34 skips now print reasons (`-rs` added); no compiler warnings beyond toolchain noise (Node 20 / setup-java deprecations, punycode); website reports `2 vulnerabilities (1 moderate, 1 high)` in its own npm audit — tracked for the website workstream; docker-build layers cache as designed.

## 5. Recovery procedure for the poisoned rows — **requires explicit approval**

All steps are prepared, none executed. Run AFTER the fixed worker image is deployed.

```sql
-- (a) The 12 PHOTO_EXT_INVALID jobs: evidence-verified requeue.
--     Safety: fence FAILED + attempts=0 + no fence target id + mapping
--     target NULL ⇒ Telegram never accepted anything; the fixed pipeline
--     (fallback → re-upload with extension) will deliver exactly once.
UPDATE delivery_jobs SET status='PENDING', next_retry_at=0, lease_until=0,
       error_detail='', updated_at=strftime('%s','now')
WHERE status='FAILED' AND error_detail LIKE '%PHOTO_EXT_INVALID%';
-- (b) The 10 rule-family UNKNOWN jobs: let the deployed reconciler close
--     them to FAILED automatically (it classifies Unimplemented as
--     provably unexecuted), then requeue with the same statement pattern
--     (error_detail LIKE '%reconciled:%').
-- (c) The 3 e2e-go-* UNKNOWN artifacts: DELETE (test contamination; the
--     fixed run_matrix now prevents recurrence).
DELETE FROM delivery_jobs WHERE id LIKE 'e2e-go-%';
-- (d) The 1 CHAT_ADMIN_REQUIRED job: NOT recoverable by retry — the
--     target's admin must grant the sending account permission first.
```

Duplicate-delivery risk per group: (a) zero — Telegram rejected every attempt server-side; (b) zero — the RPC never reached a handler; (c) n/a (no send ever associated); (d) retry adds no dup risk but cannot succeed until permissions change.

## 6. Remaining external blockers / follow-ups

1. **Deployment** of the fixed worker image (auto-deploys on merge; manual via `cd` workflow_dispatch) — user-gated.
2. **Recovery execution** (§5) — user-gated.
3. G2 decorative add-ons: implement-or-enforce decision (product-level).
4. Duplicate Filters worker-side story (was removed for cause; Python ingest still owns it).
5. Single-engine features (delay / sync_replies / sync_edits/deletes) strangler-fig completion.
6. Readiness depth (fence/runtime probes), `/metrics` exposition, scheduler metrics stub, unauthenticated :8090 controls (runbook-mitigated) — observability follow-ups, ledgered in the audit.
7. Website npm audit (1 high, 1 moderate) — website workstream.
