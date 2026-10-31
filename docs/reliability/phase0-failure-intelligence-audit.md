# Phase 0 — Repository and Production Audit (Failure Intelligence Mission)

**Scope:** AutoTelegramForward master `9354200` + open PR #278 (`fix/poll-gap-cursor-loop`).
**Method:** static review of the live code paths (no production state touched),
plus live log/DB inspection from the running `atf-core` container.

---

## 1. What already exists (do NOT rebuild)

The codebase already has substantial pieces of this mission. The audit's first
finding is that duplicating them would be an active regression.

| capability | location | state |
|---|---|---|
| **Normalized Telegram error model** | `core/infrastructure/telegram/errors.py` (624 lines) | **Implemented.** `TelegramErrorEvent` carries RPC code, error type, raw message (redacted), wait value, method, operation, chat/session/rule, attempt, correlation id, category, severity, retryable, outcome. `extract()` is total — never raises, never fabricates. |
| **Deterministic classification table** | `errors.py::_TABLE` (~70 pyrogram exception classes) | **Implemented.** MRO walk; unknown RPC errors stay `err_rpc_unknown` rather than being mapped onto a wrong category. |
| **One classification policy** | `core/infrastructure/telegram/failure_policy.py` (198 lines) | **Implemented.** `classify_failure()` answers account / permanent / transient / unknown. The queue already routes on it (auto-pause, retry, DLQ). |
| **Fenced per-target delivery state** | `delivery_attempts` table (schema v31), `DurableQueueManager` | **Implemented (PR #274).** Atomic claim *before* any Telegram call, keyed on the business deliverable. |
| **Ingestion gap recovery** | `ingestion_gaps` table, `ChannelPollingService._recover_gaps` | **Implemented (PR #274)**, and **fixed for the cursor-loop regression this week** (PR #278, open, 13/13 CI). |
| **Raw-API observability for ingestion** | PR #278 (`_emit` on the poller) | **Implemented.** `INGESTION_GAP_CREATED/RECOVERED/EXHAUSTED/PERMANENT_FAILURE` events with the raw Telegram error attached. |
| **Reconciliation** | `core/application/delivery_reconciler.py` (PR #276, merged) | **Implemented.** Six checks, owner-scoped, both engines, surfaced on `GetDeliveryStats` + `/api/queue`. |
| **Capability matrix (verified against live Telegram)** | `docs/reliability/telegram-content-capability-matrix.md` (PR #275) | **Documented and probe-verified** — `poll`/`contact` `copy_message` both fail on the real API. |
| **Raw update tracer** | `core/infrastructure/telegram/raw_tracer.py` (298 lines) | **Implemented.** Env-gated, sanitized summaries of raw MTProto updates. |
| **Central structured async logger** | `core/observability/log_client.py` + Rust logger | **Implemented.** Bounded queue, in-flight accounting, bearer-token gate, drop-counter. |
| **Centralized logging contract** | `LogClient.error/info/warn(service, event, message, detail)` | **Implemented.** Python producers + Rust ingestion already agree on it. |

## 2. Where the actual gaps are

These are the gaps this mission *should* close. Each is a measured absence,
not an assumption.

### GAP-1 — the delivery path does not persist the structured error
`durable_queue` **reads** the normalized model (`classify_failure`,
`tg_error`, `tg_detail`) and logs plain-text `logger.error(...)` lines, plus
`self._log_client.error(...)` at ~8 points — but what it *persists* on the job
is a single free-text column:

```
UPDATE delivery_jobs SET ... error_detail = ?  -- one TEXT field
```

So `delivery_jobs.error_detail` today is a paraphrase (`"RuntimeError 500
INTERNAL"`), not the structured `(error_origin, rpc_code, rpc_type,
rpc_message, category, reason, retryable, fallback_eligible, operation,
strategy, raw_exception_class)` the mission requires. The error *model* exists
and is correct; **it is simply not wired to persistence**.

### GAP-2 — no per-attempt diagnostic history
`delivery_attempts` has `last_error TEXT` — one field, overwritten on every
attempt. There is no record of *which strategy* was tried, what Telegram
returned for it, or why the fallback was selected. The mission's §6.3
(*"successful fallback must retain its preceding failure history"*) is
unachievable without an attempt-history table.

### GAP-3 — the taxonomy is operation-flat, not stage-aware
`errors.py` categories are `login | forward | ai | auth | bot | database |
system` — coarse facets for the in-bot Log Center. The mission's pipeline
stages (PARSE / MEDIA / ROUTE / ENQUEUE / CLAIM / SEND / SYNC / RECONCILE) are
not represented anywhere, so an operator cannot distinguish *"the parser lost
the caption"* from *"Telegram rejected the copy"* from the persisted state.

### GAP-4 — parser and media failures have no diagnostic identity
A parse failure currently surfaces as a pipeline `logger.exception` and a
dropped message. There is no `PARSE_FAILED`, `MEDIA_DOWNLOAD_FAILED`,
`MEDIA_UPLOAD_FAILED` classification, no parser-version field, and no fidelity
check (*"caption was present in the source and is absent in the payload"*).

### GAP-5 — fallback decisions are not visible as events
The queue does implement copy → re-upload → text-reconstruction fallback (and
`_on_restricted` gating), but the *transitions* are `logger.info` lines, not
structured `delivery.fallback.selected` events with the triggering raw error
and the policy reason. A successful fallback therefore leaves no auditable
trace of the original failure.

### GAP-6 — ingestion gap cause is only partly structured
PR #278 emits the four gap lifecycle events, but the recorded *cause* is
single-valued (`"id not returned"`). The mission's §7 distinction —
*does-not-exist vs inaccessible vs partial-response vs parser-refused vs
persisted-but-not-dispatched vs dispatched-but-not-delivered vs unknown* — is
not yet represented.

### GAP-7 — monitoring metrics are aggregates, not per-facet
`/api/queue` and the reconciler expose totals (pending/delivered/failed/rates).
The mission's §10 list (per-stage error rates, fallback success rate, media
upload/download failures, parser failures, FloodWait events) needs aggregates
*by category and stage*, which requires the persisted structured event.

## 3. Architecture alignment (§2 ownership)

Go owns the public API, task lifecycle, routing orchestration, retry policy
and the monitoring surface. Rust owns log ingestion/persistence/query.
Python owns Telegram execution and reports through the existing contracts.

**This audit confirms the correct split for the new work:**

* the **error taxonomy and diagnostic event contract** is a *Go-owned* canonical
  definition in the protobuf, with Python producing the events and Rust
  ingesting them (additive, no client breakage);
* Python's `errors.py` stays the **execution-level extractor** (it already is);
  what is added is the *persistence bridge* in Python and the *contract* in Go;
* Rust's logger **already** consumes the `LogClient` contract — no second log
  store is needed, only new event codes the logger persists generically.

## 4. What will NOT be done

* No rewrite of `errors.py` / `failure_policy.py` — extend, do not replace.
* No new logging database. No new transport. No second taxonomy.
* No content-capability re-derivation — the probe-verified matrix exists.
* No change to the Go ownership of API contracts.

## 5. Relationship to PR #278

PR #278 (open, 13/13 CI) is a **prerequisite**, not a duplicate: it fixes the
live cursor-loop regression and introduces the poller's `_emit` structured
emission that this mission's §3 work generalizes. It should merge first.
