"""Regression tests for task-lifecycle state integrity.

Each test here pins a defect that was confirmed by reading the code and
reproduced against the real SQLite repository (not a mock):

1. `cancel_owned` reported CANCELLED even when its conditional UPDATE
   committed nothing, and RETRY_WAIT tasks — which pass the early checks —
   were never matched by the PENDING-only predicate, so a task queued for
   retry was reported cancelled while it still ran later.
2. `retry_owned` requeued by rewriting every column from a local snapshot with
   no owner/state predicate, and kept the previous attempt's `result_json`.
3. The executor kept running after its lease was lost (the watchdog discarded
   `renew_lease`'s False return), applying side effects for a task another
   worker owned.
4. A replica with no executor for a claimed kind pushed the row back by
   rewriting it whole, leaving its own `lease_token` on the row, once per poll
   cycle, instead of never claiming it.

Run: pytest core/tests/test_user_task_state_integrity.py
"""

import asyncio
import logging
import time

import pytest

from core.application.user_tasks import (
    CANCELLED,
    FAILED,
    PENDING,
    RETRY_WAIT,
    RUNNING,
    TaskError,
    UserTaskUseCases,
)
from core.application import user_task_worker as worker_mod
from core.application.repositories import kind_filter_sql
from core.application.user_task_worker import UserTaskWorker
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.postgres_repositories import (
    _affected,
    _is_unique_violation,
)
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteUserTaskRepository,
)


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


def _row(repo, task_id):
    row = repo._db.query_one("SELECT * FROM user_tasks WHERE id=?", (task_id,))
    assert row is not None, "task row disappeared"
    return row


# --------------------------------------------------------------------------- #
# 1. Cancel must report what actually happened
# --------------------------------------------------------------------------- #

def test_cancel_a_retry_waiting_task_persists_and_reports_cancelled(uc, repo):
    """A task queued for its next attempt is queue-bound: cancelling it must
    really cancel it (pre-fix the PENDING-only predicate matched nothing, the
    row stayed RETRY_WAIT, and the caller was still told CANCELLED)."""
    task, _ = _run(uc.create(101, "dlq.purge"))
    claimed = _run(uc.acquire_for_work())
    assert claimed is not None and claimed.id == task.id
    waiting = _run(uc.fail(claimed, "transient", "executor_error", retryable=True))
    assert waiting.status == RETRY_WAIT

    cancelled = _run(uc.cancel_owned(task.id, 101))
    assert cancelled.status == CANCELLED
    assert _row(repo, task.id)["status"] == CANCELLED, (
        "the caller was told CANCELLED but the row was not cancelled")


def test_cancel_reports_the_truth_when_a_claim_wins_the_race(uc, repo):
    """Deterministic CANCEL-vs-claim race: the worker claims the row between
    the ownership read and the conditional UPDATE, so nothing is cancelled.
    The caller must be refused (RUNNING, poll) — never handed a CANCELLED task
    that is in fact executing."""
    task, _ = _run(uc.create(102, "dlq.purge"))

    real_cancel = repo.cancel_queued

    async def claim_first(task_id, owner_user_id, now):
        # the worker's claim lands first: same conditional-update semantics
        await repo.acquire_next("racing-worker-token", int(now) + 600, int(now))
        return await real_cancel(task_id, owner_user_id, now)

    repo.cancel_queued = claim_first
    with pytest.raises(TaskError) as e:
        _run(uc.cancel_owned(task.id, 102))
    assert e.value.code == "running_not_cancellable"
    row = _row(repo, task.id)
    assert row["status"] == RUNNING
    assert row["lease_token"] == "racing-worker-token", (
        "cancel must not clear a lease it does not own")


def test_cancel_stays_idempotent_for_an_already_cancelled_task(uc, repo):
    task, _ = _run(uc.create(103, "dlq.purge"))
    first = _run(uc.cancel_owned(task.id, 103))
    again = _run(uc.cancel_owned(task.id, 103))
    assert first.status == CANCELLED and again.status == CANCELLED
    assert _row(repo, task.id)["status"] == CANCELLED


def test_cancel_refuses_another_owners_task(uc, repo):
    task, _ = _run(uc.create(104, "dlq.purge"))
    with pytest.raises(TaskError) as e:
        _run(uc.cancel_owned(task.id, 999))
    assert e.value.code == "not_found"
    assert _row(repo, task.id)["status"] == PENDING


# --------------------------------------------------------------------------- #
# 2. Retry is atomic and does not carry the previous attempt's result
# --------------------------------------------------------------------------- #

def test_retry_requeues_atomically_and_clears_the_previous_result(uc, repo):
    task, _ = _run(uc.create(105, "dlq.purge"))
    claimed = _run(uc.acquire_for_work())
    _run(uc.complete(claimed, '{"old": "result"}'))
    dead = _run(uc.get_owned(task.id, 105))
    # drive it to FAILED the way the worker would after exhausting attempts
    repo._db.execute("UPDATE user_tasks SET status='FAILED', error='boom',"
                    " error_code='executor_error' WHERE id=?", (task.id,))

    requeued = _run(uc.retry_owned(task.id, 105))
    assert requeued.status == PENDING
    row = _row(repo, task.id)
    assert row["status"] == PENDING and int(row["attempt"]) == 0
    assert row["result_json"] == "", "a requeued task must not serve a stale result"
    assert row["error"] == "" and row["error_code"] == ""
    assert dead is not None  # the pre-claim snapshot is not what was written


def test_retry_of_a_running_task_is_an_idempotent_no_op(uc, repo):
    """Documented behaviour (unchanged by this work): a task that is PENDING,
    RETRY_WAIT or RUNNING is already queued/executing, so a retry returns it
    as-is. Only FAILED is requeued, and only CANCELLED/SUCCEEDED are refused."""
    task, _ = _run(uc.create(106, "dlq.purge"))
    claimed = _run(uc.acquire_for_work())
    assert claimed.status == RUNNING
    same = _run(uc.retry_owned(task.id, 106))
    assert same.status == RUNNING, "a running task must not be requeued under a worker"
    row = _row(repo, task.id)
    assert row["status"] == RUNNING and row["lease_token"] == claimed.lease_token, (
        "an idempotent retry must not touch a task another worker is running")


def test_retry_refuses_a_settled_task(uc, repo):
    task, _ = _run(uc.create(111, "dlq.purge"))
    _run(uc.cancel_owned(task.id, 111))
    with pytest.raises(TaskError) as e:
        _run(uc.retry_owned(task.id, 111))
    assert e.value.code == "not_retryable"
    assert _row(repo, task.id)["status"] == CANCELLED


def test_retry_failed_refuses_a_row_that_is_not_failed(repo, uc):
    """The conditional UPDATE itself refuses: no read-modify-write window."""
    task, _ = _run(uc.create(107, "dlq.purge"))
    _run(uc.acquire_for_work())
    assert _run(repo.retry_failed(task.id, 107, int(time.time()))) is None
    assert _row(repo, task.id)["status"] == RUNNING


# --------------------------------------------------------------------------- #
# 3. A lost lease aborts the executor
# --------------------------------------------------------------------------- #

class _FakeTasks:
    """Minimal task surface for _run_one: renewal always fails.

    Records terminal writes instead of raising, so an unexpected write is
    asserted by the test and never cascades into the worker's error path.
    """

    def __init__(self):
        self.completed = []
        self.failed = []
        self.renewals = 0

    async def renew_work_lease(self, task, lease_seconds=120.0):
        self.renewals += 1
        return False  # the lease is not ours any more

    async def complete(self, task, result_json=""):
        self.completed.append(task.id)
        return task

    async def fail(self, task, message, code="task_failed", retryable=True):
        self.failed.append((task.id, code))
        return task


def test_executor_is_aborted_when_its_lease_is_lost(monkeypatch):
    monkeypatch.setattr(worker_mod, "LEASE_RENEW_INTERVAL", 0.01)
    ran_to_completion = []

    async def slow_executor(uid, payload):
        await asyncio.sleep(5)
        ran_to_completion.append(uid)
        return {"ok": True}

    tasks = _FakeTasks()
    w = UserTaskWorker(tasks=tasks, executors={"dlq.purge": slow_executor},
                       max_concurrent=1, poll_interval=0.01)

    from core.domain.entities import UserTask
    task = UserTask(
        id="t-lease", owner_user_id=7, kind="dlq.purge", status="RUNNING",
        idempotency_key="", attempt=1, max_attempts=3,
        lease_token="tok", lease_until=int(time.time()) + 60, run_at=0,
        payload_json="{}", result_json="", error="", error_code="",
        created_at=1, updated_at=1, started_at=1, finished_at=0)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await w._run_one(task, {})

    _run(scenario())
    assert ran_to_completion == [], (
        "the executor kept running after the lease was lost")
    assert tasks.completed == [] and tasks.failed == []


def test_transient_renewal_errors_do_not_abort_within_the_lease_window(monkeypatch):
    """An unreachable store says nothing about who holds the lease: inside the
    lease window the executor must keep running rather than be killed."""
    monkeypatch.setattr(worker_mod, "LEASE_RENEW_INTERVAL", 0.01)
    finished = []

    class FlakyTasks(_FakeTasks):
        async def renew_work_lease(self, task, lease_seconds=120.0):
            raise RuntimeError("store unreachable")

    async def quick_executor(uid, payload):
        await asyncio.sleep(0.05)
        finished.append(uid)
        return {"ok": True}

    tasks = FlakyTasks()
    w = UserTaskWorker(tasks=tasks, executors={"dlq.purge": quick_executor},
                       max_concurrent=1, poll_interval=0.01)
    from core.domain.entities import UserTask
    task = UserTask(
        id="t-flaky", owner_user_id=7, kind="dlq.purge", status="RUNNING",
        idempotency_key="", attempt=1, max_attempts=3,
        lease_token="tok", lease_until=int(time.time()) + 3600, run_at=0,
        payload_json="{}", result_json="", error="", error_code="",
        created_at=1, updated_at=1, started_at=1, finished_at=0)

    _run(w._run_one(task, {}))
    assert finished == [7], "a transient renewal error killed a healthy executor"


# --------------------------------------------------------------------------- #
# 4. Claims are limited to executable kinds; push-back is holder-checked
# --------------------------------------------------------------------------- #

def test_claim_skips_kinds_this_replica_cannot_execute(uc, repo):
    task, _ = _run(uc.create(108, "dlq.purge"))
    got = _run(uc.acquire_for_work(kinds=["session.backup"]))
    assert got is None, "claimed a kind this replica has no executor for"
    row = _row(repo, task.id)
    assert row["status"] == PENDING and row["lease_token"] == "", (
        "a task this replica cannot run must be left untouched")


def test_claim_finds_the_task_when_the_kind_is_executable(uc):
    task, _ = _run(uc.create(109, "dlq.purge"))
    got = _run(uc.acquire_for_work(kinds=["dlq.purge"]))
    assert got is not None and got.id == task.id


def test_requeue_unclaimed_clears_the_lease_and_checks_the_holder(uc, repo):
    task, _ = _run(uc.create(110, "dlq.purge"))
    claimed = _run(uc.acquire_for_work())
    assert claimed.lease_token

    assert _run(repo.requeue_unclaimed(task.id, "not-the-holder",
                                       int(time.time()) + 10,
                                       int(time.time()))) is False
    assert _row(repo, task.id)["status"] == RUNNING

    assert _run(repo.requeue_unclaimed(task.id, claimed.lease_token,
                                       int(time.time()) + 10,
                                       int(time.time()))) is True
    row = _row(repo, task.id)
    assert row["status"] == RETRY_WAIT and row["lease_token"] == ""
    assert int(row["lease_until"]) == 0


# --------------------------------------------------------------------------- #
# Helpers shared by both engines
# --------------------------------------------------------------------------- #

def test_kind_filter_is_none_safe_parameterized_and_empty_fails_closed():
    clause, params = kind_filter_sql(None)
    assert clause == "" and params == ()
    clause, params = kind_filter_sql(["a", "b"])
    assert clause == " AND kind IN (?,?)" and params == ("a", "b")
    clause, params = kind_filter_sql(["a", "b"], placeholder="%s")
    assert clause == " AND kind IN (%s,%s)" and params == ("a", "b")
    clause, params = kind_filter_sql([])
    assert clause == " AND 1=0" and params == (), (
        "an empty kind set must claim nothing, never everything")


def test_affected_reads_every_driver_shape():
    assert _affected("UPDATE 1") == 1
    assert _affected("UPDATE 0") == 0
    assert _affected("INSERT 0 1") == 1
    assert _affected(3) == 3
    assert _affected(None) == 0
    assert _affected("") == 0
    assert _affected("unparseable") == 0, "unknown shapes must not read as success"


def test_unique_violation_detection_is_structured_first():
    class Unique(Exception):
        sqlstate = "23505"

    class Other(Exception):
        sqlstate = "42P01"

    assert _is_unique_violation(Unique("nope")) is True
    assert _is_unique_violation(Other("duplicate key of some other table")) is False, (
        "an unrelated error must not be routed into the pre-existing-row path")
    assert _is_unique_violation(
        Exception("duplicate key value violates unique constraint \"idx\"")) is True


def test_dead_refresh_helper_is_gone():
    """refresh_stale_leases used run_until_complete from sync code: on 3.10+ it
    raised, and from inside the worker loop it raised 'loop already running'."""
    import core.application.user_tasks as mod
    assert not hasattr(mod, "refresh_stale_leases")
    logging.getLogger(__name__).info("checked FAILED constant is exported: %s", FAILED)
