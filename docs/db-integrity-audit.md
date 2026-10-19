# Database integrity audit — errors and warnings

A read-only audit of the running stores, the defects it found, and the repairs.
Run it any time with the scripts named at the end; nothing here needs a downtime
window.

## Method

The live SQLite store (`/app/data/atf.db` inside `atf-core`) was inspected
read-only (`file:...?mode=ro`): `PRAGMA integrity_check`, `PRAGMA
foreign_key_check`, `PRAGMA journal_mode/page_count/freelist_count/auto_vacuum/
user_version/foreign_keys`, row counts per table, and targeted scans for the
states that mean "the engine is lying about something":

* jobs claiming a lease that already expired (zombies),
* jobs past their retry time by more than an hour (stuck),
* jobs with `attempts > max_attempts` (impossible counters),
* jobs and mappings whose `rule_id` no longer exists (orphans),
* `forward_rules` / `bot_tokens` rows with no owner.

The same checks are what the Python test suite exercises, so a defect can be
reproduced in CI rather than only observed in production.

## What the audit found

| Finding | Verdict |
|---------|---------|
| `PRAGMA integrity_check` | **ok** |
| `PRAGMA foreign_key_check` | **clean** (0 violations) |
| Zombie claims, overdue retries, stuck jobs, `attempts > max_attempts` | **none** |
| `forward_rules` / `bot_tokens` with no owner | **none** |
| **Orphan rows from deleted tasks** | **7 rows** — `message_map` 4, `processed_messages` 3, across 2 deleted tasks (`40987af9…`, `a91823c7…`) |
| `rule_stats` declares `FOREIGN KEY (rule_id) REFERENCES forward_rules(id) ON DELETE CASCADE` | **dead declaration** — the connection runs with `PRAGMA foreign_keys = 0`, so no cascade ever fires |
| `delivery_jobs` holding one `FAILED` job | **expected**: it exhausted its retries against the revoked session `93e5e59e` (`No live client`) and is in the dead-letter queue |

## The defect behind the orphans

`IForwardRuleRepository.delete()` removed the `forward_rules` row and nothing
else, in **both** stores. The task's bookkeeping stayed:

* `message_map` / `processed_messages` / `content_dedup` — dedupe and
  idempotency records. A leftover row *claims the task already delivered* a
  source message, so a task deleted and re-created with the same id (restore
  from backup, re-import, id reuse) **silently skips that message**. That is a
  dropped forward with no error anywhere — exactly the class of bug the delivery
  invariants exist to prevent.
* `delivery_jobs`, `dead_letter_queue`, `rule_stats`, `error_log` — they grow
  without bound and keep a deleted task visible in every operator view that reads
  them.

The declared `ON DELETE CASCADE` would have covered `rule_stats`, but it never
runs (`foreign_keys = 0`), so the cleanup has to be explicit.

## The fix

* **SQLite** (`SqliteForwardRuleRepository.delete`) — the rule row and every
  dependent table are removed inside one `run_in_transaction` unit. Each
  repository write otherwise commits per statement, so a crash between "rule
  gone" and "mappings gone" would produce precisely the inconsistency being
  fixed, and nothing reconciles those tables. A table an older store does not
  have is skipped, not fatal.
* **Postgres** (`PostgresForwardRuleRepository.delete`) — one statement with
  data-modifying CTEs (`WITH deleted AS (DELETE … RETURNING id), mappings AS …`),
  so there is **no window at all**: either the rule and its bookkeeping are gone
  or neither is. The row count comes back from the statement, so a missing task
  still reports `False`.
* **The one-time repair** for stores written before the fix —
  `scripts/prune_deleted_task_rows.py` (report by default, `--apply` to remove).
  It only touches rows whose `rule_id` is absent from `forward_rules`: no live
  task's data is modified. Applied to the live store: **7 orphan rows removed**,
  re-run reports `no orphan rows: every dependent row belongs to a live task`,
  and `integrity_check` is still `ok`.

## Warnings fixed in the same pass

* `core/tests/test_p2_ai_engine.py` drove the transformer's timeout path through
  an auto-created `AsyncMock` repository: the mock answered `get_owned` with a
  MagicMock "config" whose attributes are mocks, so the prompt builder received a
  coroutine it never awaited — a `RuntimeWarning` **and** a test that no longer
  exercised the real path. The double now answers `get_owned` the way a real
  repository does; the suite runs clean under
  `-W error::RuntimeWarning`.

## Known and deliberately unchanged

* **`PRAGMA foreign_keys = 0`.** Turning it on now would change delete
  behaviour for every existing store mid-flight (and silently start cascading
  where code has always done it explicitly). The explicit transactional cleanup
  is the contract; the pragma stays as it is, and the dead `ON DELETE CASCADE`
  declaration is recorded here so nobody mistakes it for protection.
* **`delivery_jobs.status = FAILED` for a revoked session** (`93e5e59e`,
  `AUTH_KEY_UNREGISTERED`). The job is correctly terminal and dead-lettered; the
  account needs the user to log in again. Not a database defect.
* **Postgres has no `processed_messages` table** (dedupe there rides on
  `message_map`), which is why the Postgres statement cleans the tables it
  actually has.

## Reproduce / maintain

```bash
# live store, read-only: integrity, orphans, zombies, pragmas
docker exec atf-core python /tmp/db_audit.py /app/data/atf.db

# orphans from deleted tasks: report, then repair
docker exec atf-core python /tmp/prune_deleted_task_rows.py --db /app/data/atf.db
docker exec atf-core python /tmp/prune_deleted_task_rows.py --db /app/data/atf.db --apply

# the regression tests
docker run --rm -v "$PWD":/repo -w /repo -e ATF_SRC_ROOT=/repo atf-pytest:git \
  python -m pytest core/tests/test_task_delete_bookkeeping.py -q
```
