# Phase 2 — API-First Multi-Tenant Architecture

Status: implemented on `feature/api-first-phase2`. The authoritative public
entry point is the Go API at `/api`; the Python Core keeps Telegram execution
and the gRPC internal plane, Rust stays the logger, Redis stays a
performance/coordination accelerator only.

## 1. Identity and tenant model

| platform | identity source | notes |
| --- | --- | --- |
| Web dashboard | Bearer JWT from `/api/auth/login` | dashboard never sends an `owner_user_id`; it is the token's |
| Telegram bot | admin/staff verification then `auth/exchange-bot-token` | callback-menu identity |
| Telegram Mini App | `initData` (signature-verified) → `/api/auth/tma` → same JWT | the identity the bot surface already tests against |
| Flutter | login → Bearer JWT (refreshable) + `X-Session-Id` in the portal session header | `ApiClient` injects the header on every request |
| Admin | Bearer JWT with `ADMIN_USER_ID` + password gate | the same endpoints, strictly admin-scoped |

Rules enforced in code (not by convention):

- **Identity binding** — `owner_user_id` is taken from the verified JWT at the
  Go API (`api/internal/handlers/identity.go` `requireUser`), never from the
  request body. Core-side, the same derivation happens at the gRPC boundary —
  the task servicer reads the identity stamped by the Go side, and refuses
  foreign-tenant access with the *same shape* as a missing resource
  (`GET /api/tasks/{id}` of another user is a `404` with
  `{"error": "not_found"}`), an existence oracle is not provided.
- **Tenant-scoped queries** — every repo read/write takes `owner_user_id`
  from the verified identity (SQLite `WHERE owner_user_id = ?`; PostgreSQL
  `... owner_user_id = $n`), no string interpolation, parameterized.
- **Sessions / XIH / audit platform binding** — the client portal session id
  rides `X-Session-Id` and the download constructs go through the API rather
  than being constructed client-side.

## 2. Per-user task lifecycle (`/api/tasks`)

State machine: `PENDING → RUNNING → RETRY_WAIT → PENDING … →
SUCCEEDED | FAILED | CANCELLED`. Transitions are validated by
`core/application/user_tasks.py` (`can_transition`) and executed as
conditional, holder-checked `UPDATE`s on the `user_tasks` table
(`finish_succeeded`, `finish_failed`, `pause_for_retry`, `cancel_queued`).
Redis is NOT a part of the task store — the DB is authoritative and the task
claims use the database's atomicity, so a Redis outage does not corrupt task
state (only the delivery queue keeps using Redis leases, as before).

Key endpoints (all authenticated, tenant-scoped):

- `POST /api/tasks` — idempotent submission. Same `(owner, kind,
  idempotency_key)` returns the original task (`200`, `already_existed: true`)
  instead of a duplicate row (partial unique index `idx_user_tasks_idem`).
  Payload is bounded (64 KiB) and validated.
- `GET /api/tasks` — the owner's jobs (filterable by `status`/`kind`,
  bounded `limit`, `offset`); `total` for pagination.
- `GET /api/tasks/{id}` — poll state, structural errors per-attempt.
- `POST /api/tasks/{id}/cancel` — one conditional UPDATE, wins/loses against
  a concurrent claim (never both).
- `POST /api/tasks/{id}/retry` — bounded attempts reset to `PENDING` with
  negative run_at nudge; effort is bounded by `max_attempts`.

Task kinds (allowlisted at every hop):

- `session.backup` — `payload.session_id` (owned by the caller)
- `dlq.replay` — `payload.delivery_id` (rule ownership re-verified by Core)
- `dlq.purge` — `payload.rule_id` or `"all"` (scoped to the caller's rules)
- `backup.export` — no payload; credential bundle is not stored in the task
  result; only the counts are (`bundle: "__delivered_synchronously__"`).

A regression-contract test (`scripts/tests/test_task_kind_parity.py`) pins the
kind set the API accepts to the kind set Core implements, so a new kind ships
with both sides updated or nothing.

## 3. Audit trail

Task lifecycle milestones are recorded into the durable PostgreSQL audit
store (`audit_events` + transactional outbox, same pipeline established in
Phase Five): `user_task.succeeded`, `user_task.failed`,
`user_task.outcome_unconfirmed` with `task_id`, `tenant_id`,
`attempt/max_attempts` metadata. SQLite deployments (no central store) keep
the existing lifecycle INFO logging. The audit helper never raises — audit
must not be able to abort a task.

## 4. Worker

`core/application/user_task_worker.py` claims through
`acquire_for_work` (atomic `UPDATE ... RETURNING`, bounded lease
`reclaim_expired`), executes with a fixed-size semaphore
(`config.task_worker_concurrency`, default 4), renews the lease every 60 s and
executors reuse the exact same domain calls the synchronous endpoints use:
`build_executors(container)` wires kinds to `backup.create_snapshot`,
`session_repo.get_owned`, and the owner-scoped DLQ replay/purge. Multiple Core
replicas coordinate purely on the database claim; no extra Redis lock is
required for correctness.

## 5. Cross-tenant tests

`core/tests/test_user_task_lifecycle.py` (21 tests) builds realistic SQLite
fixtures with **two or more tenants** and asserts, per scenario:

- succeed
- cross-tenant read blocked → same shape as missing
- cross-tenant cancel/retry blocked
- double submit → duplicate submission returns the SAME task
- bounded claims respect the concurrent limit (`max_concurrent` = 1)
- worker retry path honours `max_attempts`
- owner cannot see a foreign task in `total`

`api/internal/handlers/tasks_test.go` runs the Go identity layer against the
fake Core client (owned returns, foreign returns, bad allowlist, mismatched
owner post-response refused).

## 6. Performance characteristics (measured)

SQLite engine on this 2-vCPU host (`docs/benchmarks/user-tasks-results.json`,
tool: `python3 scripts/benchmark_user_tasks.py`):

- task submit (idempotent insert): ~36–76 µs/call from 100 → 10 000 rows
- list 50 (page-size the API serves): ~0.17 ms p50 at 100 rows; 0.36 ms at 1 000;
  2.24 ms in the 10 000-row worst case (each row carries the full payload —
  the page stays 50 rows, the cost is the tenant scan)
- claim (atomic UPDATE .. RETURNING w/ unique lease token): ~24 µs p50 at 100
  rows, ~60 µs at 1 000, ~470 µs at 10 000 (the claim ordering scan is
  bounded by the pick index `(status, run_at, created_at, id)` — production
  PostgreSQL uses its own planner and the same index pattern).

The claim/list paths are exactly the ones the Go API serves, so these are
end-to-end numbers rather than microbenchmarks of only one helper surface.
