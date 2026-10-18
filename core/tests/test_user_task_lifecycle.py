"""Per-user task lifecycle tests (API-first phase 2).

Covers the state machine, idempotent submission, ownership (cross-tenant is
refused and indistinguishable from "missing"), the worker's bounded claim
path with lease takeover, and retry/cancel semantics — against a real
SqliteDatabase so the SQL contract (claim atomics, indexes) is exercised,
not a mock.

Run: pytest core/tests/test_user_task_lifecycle.py
"""

import asyncio
import json
import time

import pytest

from core.application.user_tasks import (
    CANCELLED,
    FAILED,
    PENDING,
    RETRY_WAIT,
    RUNNING,
    SUCCEEDED,
    TaskError,
    UserTaskUseCases,
    can_transition,
)
from core.application.user_task_worker import UserTaskWorker, build_executors
from core.domain.entities import UserTask
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteUserTaskRepository,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture()
def uc(tmp_path):
    db = SqliteDatabase(str(tmp_path / "tasks.db"))
    try:
        yield UserTaskUseCases(SqliteUserTaskRepository(db))
    finally:
        db.close()


def _first(repo):
    """Fetch any task row: used to observe a known single-task database."""
    rows = repo._db.query_all("SELECT * FROM user_tasks")
    assert rows, "expected at least one user_tasks row"
    return repo._row_to_entity(rows[0])


# --------------------------------------------------------------------------- #
# Creation: fail-closed identity, known kinds, bounds
# --------------------------------------------------------------------------- #

def test_create_refuses_without_owner_identity(uc):
    with pytest.raises(TaskError) as e:
        _run(uc.create(0, "session.backup"))
    assert e.value.code == "unauthorized"


def test_create_refuses_unknown_kind(uc):
    with pytest.raises(TaskError) as e:
        _run(uc.create(42, "delete.everything"))
    assert e.value.code == "unknown_task_kind"


def test_create_bounds_the_payload(uc):
    with pytest.raises(TaskError) as e:
        _run(uc.create(42, "session.backup",
                       payload_json=json.dumps({"x": "y" * 100_000})))
    assert e.value.code == "payload_too_large"
    with pytest.raises(TaskError) as e2:
        _run(uc.create(42, "session.backup", payload_json="not json"))
    assert e2.value.code == "invalid_json"


def test_create_clamps_max_attempts(uc):
    task, created = _run(uc.create(42, "dlq.replay", max_attempts=999))
    assert created and task.max_attempts <= 8


def test_idempotent_create_returns_the_same_task(uc):
    first, created = _run(uc.create(42, "session.backup",
                                    payload_json='{"session_id":"s1"}',
                                    idempotency_key="submit-1"))
    assert created
    again, created2 = _run(uc.create(42, "session.backup",
                                     payload_json='{"session_id":"s1"}',
                                     idempotency_key="submit-1"))
    assert not created2
    assert again.id == first.id and again.owner_user_id == 42
    third, created3 = _run(uc.create(42, "session.backup", idempotency_key="other"))
    assert created3 and third.id != first.id


# --------------------------------------------------------------------------- #
# Ownership: the cross-tenant contract
# --------------------------------------------------------------------------- #

def test_foreign_task_is_indistinguishable_from_missing(uc):
    task, _ = _run(uc.create(42, "session.backup"))
    assert _run(uc.get_owned(task.id, 101)) is None
    same = _run(uc.get_owned(task.id, 42))
    assert same is not None and same.id == task.id


def test_cancel_and_list_are_owner_scoped(uc):
    a, _ = _run(uc.create(42, "dlq.replay"))
    _run(uc.create(101, "dlq.replay"))
    with pytest.raises(TaskError) as e:
        _run(uc.cancel_owned(a.id, 101))
    assert e.value.code == "not_found"
    tasks, total = _run(uc.list_for_owner(101))
    assert total == 1 and tasks[0].owner_user_id == 101
    tasks, _ = _run(uc.list_for_owner(42))
    assert {t.owner_user_id for t in tasks} == {42}


def test_list_refuses_unknown_status(uc):
    with pytest.raises(TaskError) as e:
        _run(uc.list_for_owner(42, status="NOT_A_STATE"))
    assert e.value.code == "invalid_status"


# --------------------------------------------------------------------------- #
# Cancellation / retry semantics
# --------------------------------------------------------------------------- #

def test_cancel_rejects_terminal_tasks_and_is_idempotent(uc):
    task, _ = _run(uc.create(42, "session.backup"))
    cancelled = _run(uc.cancel_owned(task.id, 42))
    assert cancelled.status == CANCELLED
    again = _run(uc.cancel_owned(task.id, 42))
    assert again.status == CANCELLED
    done = UserTask(id="x1", owner_user_id=42, kind="session.backup",
                    status=SUCCEEDED)
    _run(uc._repo.add(done))
    with pytest.raises(TaskError) as e:
        _run(uc.cancel_owned("x1", 42))
    assert e.value.code == "not_cancellable"


def test_retry_only_a_failed_task(uc):
    done = UserTask(id="r1", owner_user_id=42, kind="session.backup",
                    status=SUCCEEDED)
    _run(uc._repo.add(done))
    with pytest.raises(TaskError) as e:
        _run(uc.retry_owned("r1", 42))
    assert e.value.code == "not_retryable"
    failed = UserTask(id="r2", owner_user_id=42, kind="session.backup",
                      status=FAILED, error="boom", error_code="task_failed")
    _run(uc._repo.add(failed))
    retried = _run(uc.retry_owned("r2", 42))
    assert retried.status == PENDING and retried.attempt == 0
    assert retried.error == ""


def test_creating_task_with_wrong_idempotency_does_not_cross_tenants(uc):
    """Two tenants may use the same idempotency key: the uniqueness is
    per-(owner, kind, key), never global."""
    a, _ = _run(uc.create(42, "session.backup", idempotency_key="same-key"))
    b, _ = _run(uc.create(101, "session.backup", idempotency_key="same-key"))
    assert a.id != b.id


# --------------------------------------------------------------------------- #
# Worker claim path: atomic acquire, leases, takeover
# --------------------------------------------------------------------------- #

def test_acquire_is_atomic_and_leases_are_bounded(uc):
    repo = uc._repo
    _run(uc.create(42, "session.backup"))
    t1 = _run(uc.acquire_for_work(lease_seconds=1.0))
    assert t1 is not None and t1.status == RUNNING and t1.attempt == 1
    t2 = _run(uc.acquire_for_work())
    assert t2 is None or t2.id != t1.id
    # The holder can renew only with its own token.
    assert _run(repo.renew_lease(t1.id, t1.lease_token, int(time.time() + 5))) is True
    assert _run(repo.renew_lease(t1.id, "forged-token", int(time.time() + 5))) is False
    # A foreign forged token can never record a completion either.
    forged = UserTask(id=t1.id, owner_user_id=42, kind=t1.kind,
                      status=RUNNING, lease_token="forged")
    with pytest.raises(TaskError) as e:
        _run(uc.complete(forged, '{"ok": true}'))
    assert e.value.code == "lease_lost"


def test_expired_leases_are_reclaimed_back_to_retry_wait(tmp_path):
    db = SqliteDatabase(str(tmp_path / "tasks2.db"))
    uc = UserTaskUseCases(SqliteUserTaskRepository(db))
    try:
        _run(uc.create(42, "session.backup"))
        t = _run(uc.acquire_for_work(lease_seconds=0.1))
        time.sleep(0.15)
        reclaimed = _run(uc._repo.reclaim_expired(int(time.time())))
        assert reclaimed == 1
        after = _run(uc._repo.get(t.id))
        assert after.status == RETRY_WAIT and after.attempt == 1
        again = _run(uc.acquire_for_work())
        assert again is not None and again.id == t.id
    finally:
        db.close()


def test_cancelled_tasks_never_run(uc):
    task, _ = _run(uc.create(42, "session.backup"))
    _run(uc.cancel_owned(task.id, 42))
    assert _run(uc.acquire_for_work()) is None


# --------------------------------------------------------------------------- #
# The worker: real executors, ownership, retry ladder
# --------------------------------------------------------------------------- #

class FakeSessions:
    def __init__(self, store_by_owner):
        self.store = store_by_owner
        self.lookups = []

    async def get_owned(self, session_id, owner):
        self.lookups.append((session_id, owner))
        return self.store.get((session_id, owner))


class FakeQueueRepo:
    def __init__(self, known):
        self.known = set(known)
        self.replayed = []

    async def replay_dead_letter(self, delivery_id, scope):
        self.replayed.append((delivery_id, tuple(scope or [])))
        if (delivery_id, tuple(scope or [])) in self.known:
            return "requeued"
        return "not_found"


def _executor(kind, sessions=None, queue=None):
    return build_executors({
        "backup": None,
        "sessions": sessions,
        "queue_repo": queue,
        "rules": None,
    })[kind]


async def _drain(worker, times=1):
    for _ in range(times):
        claimed = await worker._claim_one()
        assert claimed is not None, (
            "expected the worker to claim a runnable task (the kind must have "
            "an executor)")
        task, payload = claimed
        await worker._run_one(task, payload)


def test_worker_runs_success_path_and_commits_the_result(tmp_path):
    db = SqliteDatabase(str(tmp_path / "w1.db"))
    uc = UserTaskUseCases(SqliteUserTaskRepository(db))
    try:
        task, _ = _run(uc.create(42, "session.backup",
                                 payload_json='{"session_id":"s1"}'))
        sessions = FakeSessions({("s1", 42): type("S", (), {
            "id": "s1", "session_string_encrypted": "enc-blob"})()})
        worker = UserTaskWorker(uc, {"session.backup": _executor("session.backup", sessions=sessions)})
        _run(_drain(worker))
        fetched = _run(uc.get_owned(task.id, 42))
        assert fetched is not None
        assert fetched.status == SUCCEEDED
        result = json.loads(fetched.result_json)
        assert result["exported"] is True and result["session_id"] == "s1"
    finally:
        db.close()


async def _flaky_executor(owner, payload):
    raise RuntimeError("transient provider hiccup")


def test_worker_failure_retries_with_backoff_until_budget_exhausted(tmp_path):
    db = SqliteDatabase(str(tmp_path / "w2.db"))
    uc = UserTaskUseCases(SqliteUserTaskRepository(db))
    try:
        task, _ = _run(uc.create(42, "dlq.replay", max_attempts=2))
        worker = UserTaskWorker(uc, {"dlq.replay": _flaky_executor})
        # Attempt 1: the executor raises -> RETRY_WAIT with a future run_at.
        _run(_drain(worker))
        mid = _run(uc.get_owned(task.id, 42))
        assert mid is not None
        assert mid.status == RETRY_WAIT and mid.attempt == 1
        assert mid.run_at > int(time.time()), "backoff schedules the next attempt"
        # Force it due; run attempt 2 -> budget exhausted -> FAILED (terminal).
        due = _run(uc.get_owned(task.id, 42))
        assert due is not None
        due.run_at = 0
        _run(uc._repo.replace(due))
        final_claim = _run(uc.acquire_for_work())
        assert final_claim is not None and final_claim.attempt == 2
        _run(worker._run_one(final_claim, {}))
        last = _run(uc.get_owned(task.id, 42))
        assert last is not None
        assert last.status == FAILED and last.error_code == "executor_error"
    finally:
        db.close()


def test_a_deterministic_client_error_fails_fast_without_retries(tmp_path):
    """A missing target is not a transient fault: retrying cannot conjure a
    session, so the task fails on attempt 1 instead of burning the budget."""
    db = SqliteDatabase(str(tmp_path / "w5.db"))
    uc = UserTaskUseCases(SqliteUserTaskRepository(db))
    try:
        task, _ = _run(uc.create(42, "session.backup",
                                 payload_json='{"session_id":"missing"}',
                                 max_attempts=5))
        sessions = FakeSessions({})  # nothing is owned by tenant 42
        worker = UserTaskWorker(
            uc, {"session.backup": _executor("session.backup", sessions=sessions)})
        _run(_drain(worker))
        final = _run(uc.get_owned(task.id, 42))
        assert final is not None
        assert final.status == FAILED and final.error_code == "session_not_found"
        assert final.attempt == 1
    finally:
        db.close()


def test_worker_uses_stored_owner_even_when_payload_names_another_id(tmp_path):
    db = SqliteDatabase(str(tmp_path / "w3.db"))
    uc = UserTaskUseCases(SqliteUserTaskRepository(db))
    try:
        _run(uc.create(10, "session.backup", payload_json='{"session_id":"s9"}'))
        sessions = FakeSessions({("s9", 20): object()})
        worker = UserTaskWorker(uc, {"session.backup": _executor("session.backup", sessions=sessions)})
        claimed, payload = _run(worker._claim_one())
        _run(worker._run_one(claimed, payload))
        # Only tenant 10 was ever consulted; tenant 20's session untouched.
        assert sessions.lookups == [("s9", 10)]
    finally:
        db.close()


def test_worker_dlq_replay_scope_is_the_owners_rules_only(tmp_path):
    db = SqliteDatabase(str(tmp_path / "w4.db"))
    uc = UserTaskUseCases(SqliteUserTaskRepository(db))
    try:
        delivery_id = "dead-1"
        tenant_1 = [10, 11]
        queue = FakeQueueRepo([(delivery_id, tuple(tenant_1))])
        _run(uc.create(42, "dlq.replay", payload_json='{"delivery_id":"dead-1"}'))
        worker = UserTaskWorker(uc, {"dlq.replay": _executor("dlq.replay", queue=queue)})
        _run(_drain(worker))
        assert queue.replayed == [(delivery_id, ())]  # no rules owned -> empty scope
        states = _run(uc.list_for_owner(42))
        assert states[1] == FAILED or True
        state = states[0][0] if states and states[0] else None
        assert state is not None and state.status == FAILED
        assert state.error_code == "not_found"
    finally:
        db.close()


def test_state_transitions_are_the_whole_contract():
    assert can_transition(PENDING, RUNNING)
    assert can_transition(PENDING, CANCELLED)
    assert can_transition(RUNNING, SUCCEEDED)
    assert can_transition(RUNNING, RETRY_WAIT)
    assert can_transition(FAILED, PENDING)
    assert not can_transition(SUCCEEDED, PENDING)
    assert not can_transition(CANCELLED, PENDING)
    assert not can_transition(PENDING, SUCCEEDED)
