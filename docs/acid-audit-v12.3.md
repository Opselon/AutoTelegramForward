# ACID Transaction Integrity Audit — v12.3

Worktree `~/atf-acid`, branch `fix/acid-transaction-integrity`, base `870e4ab`
(master @ v1.4.1 + PR #18).

## Severity model
- **P0**: silent data corruption / cross-tenant data exposure, reachable in
  the default deployment.
- **P1**: leaves inconsistent state on failure; needs crash at the wrong
  instant.
- **P2**: resilience/observability gap.

---

## F1 (P0) — `LoginAccount` rebinds a web account to any Telegram id

`core/infrastructure/grpc_server/account_servicer.py:213-217`

```python
if request.telegram_user_id and not row["user_id"]:
    self._db.execute(
        "UPDATE web_accounts SET user_id=?, updated_at=? WHERE username=?",
        (int(request.telegram_user_id), int(time.time()), username))
```

The intent was "link-proof when issued from the bot" — the field exists so
the *bot* can bind a freshly-minted account. But the guard is only
`not row["user_id"]`, and **the field is attacker-controlled**:
`AuthLogin` does `bindPB(w, r, &req)` straight off the JSON body
(`api/internal/handlers/auth.go:40`), and `/api/auth/login` is public.

Exploit: attacker knows (or brute-forces/guesses) a victim's *username +
password*. They log in with `{"username":"victim","password":"…",
"telegram_user_id": <attacker TG id>}`. The row becomes bound to the
attacker's Telegram id. From then on:
- `LoginAccount` with no password but `telegram_user_id=<attacker id>` finds
  the row by uid (`_find_by_uid`) and succeeds (line 192-194).
- Every owner-scoped query (`list_by_owner`, `get_owned`) now returns the
  **victim's** rules, sessions, AI configs and credentials to the attacker,
  and `BackupSession` hands over the victim's encrypted session string.

This is a cross-tenant takeover reached *through the transaction path* — the
write is single-statement atomic, but it accepts an unverified key.

**Fix**: the telegram link must only be accepted from the bot-issued proof
path (`AuthBotExchange`), never from a password login. When a password
login carries `telegram_user_id` and the account is already bound, ignore
the field. When unbound, only accept it via `IssueWebToken` (the bot flow).

---

## F2 (P1) — `enqueue_delivery` writes mapping and job in two commits

`core/infrastructure/telegram/durable_queue.py:193-205`

```python
mapping = MessageMapping(..., delivery_status=DeliveryStatus.PENDING)
await self._map.add_or_update(mapping)      # commit A
...
enqueued = await self._queue.enqueue(job)   # commit B (INSERT OR IGNORE)
```

Each `SqliteDatabase.execute()` auto-commits. A crash between A and B leaves
a `PENDING` mapping with no delivery job. The watchdog/queue scans jobs, not
mappings, so this row is orphaned forever and is silently inconsistent state.

The job insert itself is idempotent (`INSERT OR IGNORE` on a deterministic
`{rule}:{chat}:{msg}:{target}` id), so re-running `process_for_rule` cannot
double-enqueue. **The queue is safe; the mapping table is the leak.**

**Fix**: insert the job first (it is the authority), then upsert the
mapping, and record the mapping status from the enqueue result. Orphaned
`PENDING` mappings get reconciled by the existing watchdog loop.

---

## F3 (P1) — login finalize activates the row before the client is live

`core/infrastructure/telegram/login_flow.py:362-377`

```python
session = await self._sessions.create(...)   # commit A: row exists, inactive
session.activate()
await self._sessions._repo.update(session)   # commit B: row now "active"
await self._pool.start_with_session_string(...)  # external client started
```

Crash after B but before C (or C raises) → the DB says an active, authorized
session exists while no Telegram client is running. The reconciler will not
restart it (it only restarts clients for rows it deems live), so the
account's rules silently stop forwarding until a manual restart.

**Fix**: start the client first; only flip `is_active` once the client is
running. On failure, leave the row inactive and log the reason.

---

## F4 (P1) — `mark_sent` + `update_delivery` is a two-commit window

`core/infrastructure/telegram/durable_queue.py:286-296`

```python
await self._queue.mark_sent(job.id, target_msg_id)   # commit A: job SENT
await self._map.update_delivery(..., status=SENT)    # commit B: map SENT
```

Crash between A and B: the job is marked SENT so it will never be replayed,
but the mapping stays `PENDING`/`CLAIMED` forever. The dashboard then shows
a rule whose source→target link is inconsistent with its delivery state.

Not a duplicate-send bug (Telegram already has the message; the job is done
and is correctly not retried) — it is a **durability-of-state** bug: the
committed durable record and the mapping disagree permanently.

**Fix**: write the mapping first, then mark the job, so a partial state
leaves a mapping that the watchdog can reconcile. Same class as F2.

---

## F5 (P1) — no transaction API; every write is its own commit

`core/infrastructure/persistence/database.py:622-626`

```python
def execute(self, sql, params=()):
    with self._lock:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur
```

Every repository write is one auto-commit statement. Multi-step invariants
(mapping+job, activate+start, credential+session) therefore have no atomic
boundary. This is the root cause of F2/F3/F4: they cannot be fixed without
a way to group statements.

Note `claim_batch` already does the right thing — it takes `self._db._lock`
and issues SELECT + N UPDATEs under one lock and one commit. That pattern
just needs to be exposed as a reusable primitive.

**Fix**: add `transaction()` / `run_in_transaction()` to `SqliteDatabase`
that yields a cursor under the lock with no per-statement commit, committing
once on success and rolling back on exception. Keep `execute()` as-is for
back-compat; route the multi-step paths through the new primitive.

---

## F6 (P1) — migration applies all scripts then bumps schema_version once

`core/infrastructure/persistence/database.py:527-540`

```python
for version in sorted(MIGRATIONS):
    if version > current:
        self._conn.executescript(MIGRATIONS[version])   # implicit commit each
        current = version
...
self._conn.execute("UPDATE schema_version SET version = ?", (current,))
self._conn.commit()
```

`executescript()` issues an implicit COMMIT before it runs. A crash mid-loop
leaves the schema half-migrated while `schema_version` still says the old
value — on the next boot it re-runs the scripts from the *old* version,
partially re-applying idempotent ones and re-executing non-idempotent ones.

Most migrations are `CREATE TABLE IF NOT EXISTS` (idempotent), so this is
low-probability/high-confusion rather than a routine corruption.

**Fix**: bump `schema_version` inside the same loop step, right after each
script, so the recorded version always matches the applied state.

---

## F7 (P2) — swallowed exceptions hide state transitions

`core/application/pipeline.py:416-422` (mark_processed) and
`durable_queue.py:308-311` (log client) both do:

```python
try:
    await ...
except Exception:
    pass
```

`mark_processed` failing means the dedup guard will not fire on a replay,
i.e. the *idempotency* protection itself is silently disabled. Same in the
`_finalize` login path (`except Exception: pass` on `state.client.disconnect()`
is fine; the surrounding broad except that converts every failure into
`login_failed:` is acceptable).

**Fix**: log the failure with the correlation id so the operator can see
that replay protection is degraded. Do not convert to a hard failure — the
message was already enqueued and must not be lost.

---

## What is already correct (verified, do not regress)

- **Queue dedup**: `INSERT OR IGNORE` on a deterministic job id
  (`{rule}:{chat}:{msg}:{target}`) makes `enqueue` idempotent; concurrent
  processing of the same message cannot double-enqueue.
- **Atomic job claiming**: `claim_batch` does SELECT + N UPDATEs under
  `self._db._lock` with a single commit; expired leases are reclaimed by
  the same query. No double-dispatch between workers.
- **Optimistic concurrency**: `ForwardRuleRepository.update(..., expected_version)`
  uses `WHERE id=? AND version=?` and raises `ConcurrencyError` on conflict
  (read-modify-write race between the bot and the dashboard is protected).
- **processed_messages / message_map** both use `INSERT OR IGNORE` /
  `ON CONFLICT … DO UPDATE` — replay-safe.
- **Restore order** in `BackupService.restore_snapshot` inserts credentials →
  sessions → rules so FKs are satisfied; per-entity failures are counted in
  the returned stats and re-import is idempotent (skips existing ids).
- **Foreign keys** are ON (`PRAGMA foreign_keys=ON`), WAL + busy_timeout set.

## Explicitly out of scope / not claimed

- Telegram delivery is **at-least-once**, not exactly-once. The audit does
  not change that: a crash between the Telegram send and `mark_sent` will
  re-dispatch on recovery. Making it exactly-once would require a
  send-idempotency token Telegram does not provide. The fixes here ensure
  the *durable state* is self-consistent after such a crash.
- `name`/`description` on `ForwardRule` are still not schema columns
  (found in PR #18 testing) — a separate data-model issue, left alone to
  avoid an unjustified schema change here.
