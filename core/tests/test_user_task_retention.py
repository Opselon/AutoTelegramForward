"""Retention for the per-user task ledger.

The task ledger is written on every long-running user operation and, before
this, nothing ever deleted a row from it: `user_tasks` grew for the life of the
deployment. These tests pin the sweep that fixes that, and — more importantly —
the rows it must never touch.

Two properties carry the weight:

* **Live work is never swept.** PENDING, RETRY_WAIT and RUNNING rows are a
  queued task its owner is still waiting for, or one a worker is executing right
  now. Age is not a reason to delete them; the predicate has to be
  status-first, and `test_live_work_survives_any_age` is the test that fails if
  someone writes it the other way round.
* **The sweep set matches the state machine.** The repository cannot import
  `user_tasks.TERMINAL_STATES` (that module imports the repository), so the
  literal states are pinned here instead: if a status is renamed or a new
  terminal state is added, these tests fail rather than the sweep silently
  keeping — or silently deleting — the wrong rows.

Run: pytest core/tests/test_user_task_retention.py
"""

import asyncio
import logging
import re
import time

import pytest

from core.application import user_task_worker as worker_mod
from core.application.repositories import (
    TASK_PURGE_BATCH,
    TASK_RETENTION_SECONDS,
)
from core.application.user_task_worker import UserTaskWorker
from core.application.user_tasks import (
    ACTIVE_STATES,
    CANCELLED,
    FAILED,
    PENDING,
    RETRY_WAIT,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATES,
    UserTaskUseCases,
)
from core.infrastructure.persistence.database import (
    MIGRATIONS,
    SCHEMA_VERSION,
    SqliteDatabase,
)
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteUserTaskRepository,
)

DAY = 60 * 60 * 24


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture()
def repo(tmp_path):
    db = SqliteDatabase(str(tmp_path / "tasks.db"))
    try:
        yield SqliteUserTaskRepository(db)
    finally:
        db.close()


@pytest.fixture()
def uc(repo):
    return UserTaskUseCases(repo)


def _seed(repo, task_id, owner, status, age, kind="dlq.purge", attempt=0,
          finished_at_override=None):
    """Write a row in a given state directly — the sweep is about stored state,
    not about how the state was reached, and driving six states through the
    real machine would test the machine instead.

    `age` is how long ago the row's timestamps are (seconds). A terminal row
    gets `finished_at` set to that backdated instant; a live row keeps
    finished_at = 0, which is what "not finished" means in the schema.
    """
    now = int(time.time())
    ts = now - int(age)
    if finished_at_override is not None:
        finished = int(finished_at_override)
    else:
        finished = ts if status in TERMINAL_STATES else 0
    repo._db.execute(
        "INSERT INTO user_tasks (id, owner_user_id, kind, status, idempotency_key,"
        " attempt, max_attempts, lease_token, lease_until, run_at, payload_json,"
        " result_json, error, error_code, created_at, updated_at, started_at,"
        " finished_at) VALUES (?,?,?,?,'',?,3,'',0,0,'{}','','','',?,?,?,?)",
        (task_id, owner, kind, status, attempt, ts, ts, ts, finished),
    )


def _statuses(repo):
    return {
        row["id"]: row["status"]
        for row in repo._db.query_all("SELECT id, status FROM user_tasks")
    }


# --------------------------------------------------------------------------- #
# The sweep set must be the state machine's terminal set
# --------------------------------------------------------------------------- #

def test_the_repository_sweeps_exactly_the_terminal_states():
    """The literal states in the sweep must be the machine's terminal states.

    The repository builds the predicate from literals (it cannot import
    user_tasks, which imports it), so the coupling is pinned here.
    """
    assert TERMINAL_STATES == {SUCCEEDED, FAILED, CANCELLED}
    assert ACTIVE_STATES == {PENDING, RETRY_WAIT, RUNNING}
    assert not (ACTIVE_STATES & TERMINAL_STATES)

    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        .joinpath("infrastructure/persistence/sqlite_repositories.py")
        .read_text(encoding="utf-8")
    )
    # The whole sweep method: its state literals live above the DELETE.
    parts = source.split(
        "async def cleanup(self, retention_seconds: int = TASK_RETENTION_SECONDS)", 1)
    assert len(parts) == 2, "the retention sweep is missing"
    body = parts[1].split("async def reclaim_expired", 1)[0]
    assert "DELETE FROM user_tasks WHERE id IN" in body, body
    # The predicate's parameters are the three terminal states, in one order...
    assert "SUCCEEDED, FAILED, CANCELLED" in body, body
    binding = re.search(
        r'SUCCEEDED, FAILED, CANCELLED = "([A-Z_]+)", "([A-Z_]+)", "([A-Z_]+)"', body)
    assert binding, "the sweep's state literals are no longer pinned to this test"
    # ...bound to exactly the machine's terminal strings...
    assert set(binding.groups()) == TERMINAL_STATES, binding.groups()
    # ...and no active state appears anywhere in the sweep, because a status
    # predicate that mentions live work is how a sweep deletes a running task.
    for state in ACTIVE_STATES:
        assert f'"{state}"' not in body, (
            f"{state} is live work and must not appear in the sweep")


def test_each_terminal_state_is_deleted_and_each_active_state_survives(repo, uc):
    ancient = 400 * DAY
    for status in sorted(TERMINAL_STATES | ACTIVE_STATES):
        _seed(repo, f"t-{status}", 500, status, ancient)

    deleted = _run(uc.purge_terminal(TASK_RETENTION_SECONDS))

    assert deleted == len(TERMINAL_STATES), _statuses(repo)
    remaining = _statuses(repo)
    for status in TERMINAL_STATES:
        assert f"t-{status}" not in remaining, f"{status} should have been swept"
    for status in ACTIVE_STATES:
        assert remaining.get(f"t-{status}") == status, (
            f"{status} is live work and must never be swept")


def test_live_work_survives_any_age(repo, uc):
    """The safety property, stated on its own: age alone never deletes a task.

    Each live state is seeded twice — once with finished_at unset (the normal
    case) and once with a finished_at in the distant past. The second row is the
    one that matters: `finished_at > 0` is a second guard, but the *status* is
    the guarantee, and a code path that stamps finished_at before a task settles
    (a requeue, a partial write, a future "last activity" column) must not turn
    a queued or running task into a sweep candidate.
    """
    expected = {}
    for status in (PENDING, RETRY_WAIT, RUNNING):
        _seed(repo, f"old-{status}", 501, status, 4000 * DAY)
        _seed(repo, f"stamped-{status}", 501, status, 4000 * DAY,
              finished_at_override=int(time.time()) - 4000 * DAY)
        expected[f"old-{status}"] = status
        expected[f"stamped-{status}"] = status

    assert _run(uc.purge_terminal(1)) == 0
    assert _statuses(repo) == expected


def test_a_running_task_with_an_expired_lease_is_not_swept(repo, uc):
    """An expired lease is the reclaim path's business, not the sweep's: the
    task is still RUNNING and may be about to be retried."""
    _seed(repo, "expired-lease", 502, RUNNING, 900 * DAY)
    repo._db.execute(
        "UPDATE user_tasks SET lease_token='dead-worker', lease_until=? WHERE id=?",
        (int(time.time()) - 60, "expired-lease"),
    )
    assert _run(uc.purge_terminal(TASK_RETENTION_SECONDS)) == 0
    assert _statuses(repo)["expired-lease"] == RUNNING


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #

def test_only_rows_outside_the_window_are_deleted(repo, uc):
    _seed(repo, "old", 503, SUCCEEDED, 40 * DAY)
    _seed(repo, "recent", 503, SUCCEEDED, 1 * DAY)
    _seed(repo, "on-the-edge-inside", 503, FAILED, 29 * DAY)

    assert _run(uc.purge_terminal(30 * DAY)) == 1
    assert _statuses(repo) == {
        "recent": SUCCEEDED, "on-the-edge-inside": FAILED}


def test_a_positive_finished_at_is_required(repo, uc):
    """finished_at == 0 means "not finished"; such a row is never swept even if
    its status says terminal (a torn write must not become a deletion)."""
    _seed(repo, "torn", 504, SUCCEEDED, 900 * DAY, finished_at_override=0)
    assert _run(uc.purge_terminal(1)) == 0
    assert "torn" in _statuses(repo)


def test_retention_of_zero_or_less_keeps_everything(repo, uc):
    _seed(repo, "ancient", 505, SUCCEEDED, 900 * DAY)
    for retention in (0, -1):
        assert _run(uc.purge_terminal(retention)) == 0
    assert "ancient" in _statuses(repo)


def test_the_default_window_is_thirty_days():
    assert TASK_RETENTION_SECONDS == 30 * DAY


# --------------------------------------------------------------------------- #
# Bounded batches
# --------------------------------------------------------------------------- #

def test_the_sweep_is_bounded_by_the_batch_size(repo, uc):
    """A backlog must drain over several sweeps, not in one long write lock."""
    total = TASK_PURGE_BATCH + 25
    for i in range(total):
        _seed(repo, f"bulk-{i:04d}", 506, SUCCEEDED, 100 * DAY + i)

    first = _run(uc.purge_terminal(TASK_RETENTION_SECONDS))
    assert first == TASK_PURGE_BATCH, first
    second = _run(uc.purge_terminal(TASK_RETENTION_SECONDS))
    assert second == 25, second
    assert _run(uc.purge_terminal(TASK_RETENTION_SECONDS)) == 0


def test_the_oldest_rows_go_first(repo, uc):
    """Ordered by finished_at ascending (a backdated instant is a smaller
    number than a recent one), so a long backlog cannot leave the newest rows
    to be deleted first while `LIMIT` re-selects them sweep after sweep."""
    # Larger `age` = further in the past = older = deleted first.
    for i in range(TASK_PURGE_BATCH + 5):
        _seed(repo, f"age-{i:04d}", 507, FAILED, (100 + i) * DAY)
    _run(uc.purge_terminal(TASK_RETENTION_SECONDS))
    remaining = set(_statuses(repo))
    # The 5 most recent rows survive; the 500 oldest were deleted.
    assert remaining == {f"age-{i:04d}" for i in range(5)}


# --------------------------------------------------------------------------- #
# Ownership: this is maintenance, not a tenant operation
# --------------------------------------------------------------------------- #

def test_the_sweep_covers_every_owner_but_only_their_finished_rows(repo, uc):
    _seed(repo, "a-old", 601, SUCCEEDED, 60 * DAY)
    _seed(repo, "b-old", 602, FAILED, 60 * DAY)
    _seed(repo, "b-live", 602, PENDING, 400 * DAY)
    _seed(repo, "a-recent", 601, CANCELLED, 1 * DAY)

    assert _run(uc.purge_terminal(30 * DAY)) == 2
    assert _statuses(repo) == {"b-live": PENDING, "a-recent": CANCELLED}


def test_the_sweep_is_not_reachable_from_the_owner_scoped_api(repo, uc):
    """`cleanup` is deliberately not owner-scoped; the callers that ARE exposed
    to tenants must not have grown a way to invoke it."""
    import core.application.user_tasks as ut

    source = (
        pytest.importorskip("pathlib").Path(ut.__file__).read_text(encoding="utf-8")
    )
    # The only caller in the module is purge_terminal itself.
    assert source.count("self._repo.cleanup(") == 1


# --------------------------------------------------------------------------- #
# Storage: the migration and the index the sweep needs
# --------------------------------------------------------------------------- #

def test_the_retention_index_is_migrated(repo):
    applied = {
        row["name"]
        for row in repo._db.query_all("PRAGMA index_list(user_tasks)")
    }
    assert "idx_user_tasks_finished" in applied, applied
    assert SCHEMA_VERSION >= 25


def test_migration_25_adds_an_index_and_nothing_else():
    """Safe and reversible: an index costs write amplification, never data."""
    statements = [
        s.strip() for s in MIGRATIONS[25].replace("--", "\n--").split(";") if s.strip()
    ]
    creates = [s for s in statements if "CREATE INDEX" in s.upper()]
    assert creates, statements
    body = MIGRATIONS[25]
    for forbidden in ("DROP ", "DELETE ", "ALTER ", "UPDATE "):
        assert forbidden not in body.upper(), f"migration 25 must not{forbidden}"


def test_the_postgres_bootstrap_carries_the_same_index():
    """A fresh PostgreSQL install gets the index from the bootstrap, not from
    the SQLite migration list (which is never applied there)."""
    path = (
        pytest.importorskip("pathlib").Path(__file__).resolve().parents[1]
        .joinpath("infrastructure/persistence/postgres_repositories.py")
    )
    assert "idx_user_tasks_finished" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The worker's cadence
# --------------------------------------------------------------------------- #

class _FakeTasks:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.available = ["dlq.purge"]

    async def purge_terminal(self, retention_seconds):
        self.calls.append(int(retention_seconds))
        if self.fail:
            raise RuntimeError("store unavailable")
        return 0


def test_the_worker_sweeps_at_most_once_per_interval(monkeypatch):
    tasks = _FakeTasks()
    worker = UserTaskWorker(tasks, {"dlq.purge": None}, retention_seconds=1000)

    _run(worker._maybe_purge())
    assert tasks.calls == [1000], "the first idle cycle should sweep"

    _run(worker._maybe_purge())
    assert tasks.calls == [1000], "a second sweep inside the interval must not run"

    # Let the interval elapse.
    worker._next_retention = time.monotonic() - 1
    _run(worker._maybe_purge())
    assert tasks.calls == [1000, 1000]


def test_retention_zero_disables_the_sweep_entirely():
    tasks = _FakeTasks()
    worker = UserTaskWorker(tasks, {"dlq.purge": None}, retention_seconds=0)
    _run(worker._maybe_purge())
    worker._next_retention = 0
    _run(worker._maybe_purge())
    assert tasks.calls == [], "retention 0 means keep forever, not purge now"


def test_a_failed_sweep_backs_off_and_does_not_escape(caplog):
    tasks = _FakeTasks(fail=True)
    worker = UserTaskWorker(tasks, {"dlq.purge": None}, retention_seconds=1000)
    before = time.monotonic()

    with caplog.at_level(logging.ERROR):
        _run(worker._maybe_purge())  # must not raise into the worker loop

    assert tasks.calls == [1000]
    assert any("retention sweep failed" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]
    assert worker._next_retention - before >= worker_mod.RETENTION_RETRY_INTERVAL - 1, (
        "a failed sweep must back off instead of retrying every poll cycle")


def test_a_broken_claim_is_reported_once_not_every_poll(caplog):
    """An empty queue and a broken claim used to be indistinguishable."""
    calls = {"n": 0}

    class _BrokenTasks:
        available = ["dlq.purge"]

        async def acquire_for_work(self, *_a, **_k):
            calls["n"] += 1
            raise RuntimeError("database is locked")

        async def purge_terminal(self, *_a, **_k):
            return 0

        class _Repo:  # recyclable stand-in for the lease store
            async def reclaim_expired(self, _now):
                return 0

        _repo = _Repo()

    # poll_interval has a 0.2s floor (a busy loop is not a poll loop), so this
    # runs a few iterations in just over a second. 1.5s covers three polls even
    # when the loop's first wait is delayed by a loaded CI worker; the claim is
    # only that the loop keeps going, not that it hits a specific rate.
    worker = UserTaskWorker(_BrokenTasks(), {"dlq.purge": None},
                            poll_interval=0.01)

    async def scenario():
        handle = asyncio.create_task(worker.run_forever())
        await asyncio.sleep(1.5)
        worker.stop()
        handle.cancel()
        try:
            await handle
        except asyncio.CancelledError:
            pass

    with caplog.at_level(logging.WARNING):
        _run(scenario())

    assert calls["n"] >= 2, "a claim failure must not end the loop"
    reported = [r for r in caplog.records if "task claim failed" in r.getMessage()]
    assert len(reported) == 1, [r.getMessage() for r in reported]
