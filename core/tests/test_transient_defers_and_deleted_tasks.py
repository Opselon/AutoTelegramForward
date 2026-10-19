"""Two ways a queued message is lost without anyone being told.

**A deleted task keeps forwarding.** The worker used to check the rule only when
it existed (``if rule_obj:``), so a job whose task had been deleted simply fell
through to dispatch: the message the owner had just removed from the panel was
delivered anyway, and the job stayed ``PENDING`` with no error. Deleting a task
must therefore take its queued work with it — the job is dropped, visibly, with
the reason recorded.

**A transient blocker burns the delivery budget.** Paused task, rate-limited
target, time-limited account, no live client, unusable bot token — every one of
those called ``mark_retry``, which increments ``attempts`` and, at
``max_attempts`` (5), marks the job ``FAILED`` and dead-letters it as
``DELIVERY_EXHAUSTED``. A session that was offline for a few seconds destroyed a
perfectly good message and reported it as permanently failed. Those states belong
to the environment, not to the job, so they postpone it without spending an
attempt (``IDeliveryQueueRepository.defer``) and the message is still there when
the blocker clears.

Both properties are asserted against the real SQLite store and the real
repository classes; only the Telegram client is a fake.
"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from core.domain.entities import DeliveryJob, ForwardRule, TelegramSession
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryQueueRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.durable_queue import DurableQueueManager

OWNER = 5094837833
SOURCE = "-1004400615270"
TARGET = "-1003915313919"


class FakeClient:
    """Records what the worker asked Telegram to do."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name: str):
        async def _call(*args, **kwargs):
            self.calls.append((name, kwargs))
            return SimpleNamespace(id=1, message_id=1)

        return _call


class FakePool:
    """A client pool whose session is either live or offline."""

    def __init__(self, client: FakeClient | None) -> None:
        self.client = client

    def get(self, session_id: str):
        return self.client

    def is_time_limited(self, session_id: str) -> bool:
        return False


def make_rule(rule_id: str = "rule-k", **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id,
        session_id="sess-k",
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        is_active=True,
        owner_user_id=OWNER,
        **kwargs,
    )
    rule.duplicate_filters = False
    return rule


def make_job(rule_id: str = "rule-k", job_id: str = "job-k") -> DeliveryJob:
    return DeliveryJob(
        id=job_id,
        rule_id=rule_id,
        source_chat_id=SOURCE,
        source_message_id=700,
        target_chat_id=TARGET,
        payload_data={
            "session_id": "sess-k",
            "text": "delete me if the task goes",
            "delivery_transport": "USER",
        },
        max_attempts=5,
    )


def queue_for(db, pool) -> DurableQueueManager:
    return DurableQueueManager(
        queue_repo=SqliteDeliveryQueueRepository(db),
        message_map_repo=SqliteMessageMapRepository(db),
        client_pool=pool,
        rule_repo=SqliteForwardRuleRepository(db),
        num_workers=1,
    )


def seed(db, *rules) -> None:
    async def go():
        await SqliteSessionRepository(db, CryptoService("test-key")).add(TelegramSession(
            id="sess-k", phone_number="+100****0000", is_active=True,
            is_authorized=True, owner_user_id=OWNER,
        ))
        repo = SqliteForwardRuleRepository(db)
        for rule in rules:
            await repo.add(rule)

    asyncio.run(go())


def enqueue(db, job: DeliveryJob) -> None:
    asyncio.run(SqliteDeliveryQueueRepository(db).enqueue(job))


def job_row(db, job_id: str) -> dict:
    return dict(db.query_one("SELECT * FROM delivery_jobs WHERE id = ?", (job_id,)))


def dead_letters(db) -> list[dict]:
    return [dict(r) for r in db.query_all("SELECT * FROM dead_letter_queue")]


@pytest.fixture
def store(tmp_path):
    db = SqliteDatabase(os.path.join(tmp_path, "defers.db"))
    try:
        yield db
    finally:
        db.close()


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# A deleted task must not keep forwarding
# --------------------------------------------------------------------------- #

def test_a_job_whose_task_was_deleted_is_dropped_not_delivered(store):
    seed(store, make_rule())
    enqueue(store, make_job())
    run(SqliteForwardRuleRepository(store).delete("rule-k"))

    client = FakeClient()
    worker = queue_for(store, FakePool(client))
    result = run(worker._process_job("w1", make_job()))

    assert result is None, "a deleted task's job must not report a confirmed send"
    assert client.calls == [], f"the deleted task's message was delivered: {client.calls}"
    # Deleting the task takes its queued job with it (#207), so there is nothing
    # left to deliver later either.
    assert store.query_one("SELECT * FROM delivery_jobs WHERE id = ?", ("job-k",)) is None


def test_a_job_whose_task_still_exists_is_delivered(store):
    """The drop is specific to a missing task, not to every job."""
    seed(store, make_rule())
    enqueue(store, make_job())

    client = FakeClient()
    worker = queue_for(store, FakePool(client))
    result = run(worker._process_job("w1", make_job()))
    # The commit is deferred to the worker loop (v14); commit like it does.
    assert result is not None
    run(SqliteDeliveryQueueRepository(store).mark_sent_batch([result]))

    assert client.calls, "a live task's message must be delivered"
    assert job_row(store, "job-k")["status"] == "SENT"


# --------------------------------------------------------------------------- #
# Transient blockers must not spend the delivery budget
# --------------------------------------------------------------------------- #

def test_a_paused_task_survives_far_more_than_max_attempts(store):
    seed(store, make_rule(is_paused=True))
    enqueue(store, make_job())

    client = FakeClient()
    worker = queue_for(store, FakePool(client))
    for _ in range(12):                       # max_attempts is 5
        assert run(worker._process_job("w1", make_job())) is None

    row = job_row(store, "job-k")
    assert row["status"] == "RETRY_WAIT"
    assert row["attempts"] == 0, "a pause is not a failed attempt"
    assert dead_letters(store) == [], "a paused task must not dead-letter its message"
    assert client.calls == []


def test_the_message_is_still_delivered_once_the_task_is_resumed(store):
    seed(store, make_rule(is_paused=True))
    enqueue(store, make_job())
    client = FakeClient()
    worker = queue_for(store, FakePool(client))
    run(worker._process_job("w1", make_job()))

    rule = run(SqliteForwardRuleRepository(store).get_by_id("rule-k"))
    assert rule is not None
    rule.is_paused = False
    rule.paused_until = 0
    run(SqliteForwardRuleRepository(store).update(rule))

    result = run(worker._process_job("w1", make_job()))
    assert result is not None
    run(SqliteDeliveryQueueRepository(store).mark_sent_batch([result]))
    assert client.calls, "the deferred message disappeared when the task resumed"
    assert job_row(store, "job-k")["status"] == "SENT"


def test_an_offline_session_defers_and_does_not_dead_letter(store):
    """Five one-second polls used to dead-letter a message during a short outage."""
    seed(store, make_rule())
    enqueue(store, make_job())

    offline = queue_for(store, FakePool(None))
    for _ in range(12):
        assert run(offline._process_job("w1", make_job())) is None

    row = job_row(store, "job-k")
    assert row["status"] == "RETRY_WAIT"
    assert row["attempts"] == 0
    assert dead_letters(store) == []

    # The account comes back: the same job is delivered, once.
    client = FakeClient()
    online = queue_for(store, FakePool(client))
    result = run(online._process_job("w1", make_job()))
    assert result is not None
    run(SqliteDeliveryQueueRepository(store).mark_sent_batch([result]))
    assert len(client.calls) == 1
    assert job_row(store, "job-k")["status"] == "SENT"


def test_defer_writes_the_next_retry_time_and_leaves_attempts_alone(store):
    seed(store, make_rule())
    enqueue(store, make_job())
    queue = SqliteDeliveryQueueRepository(store)

    before = time.time()
    assert run(queue.defer("job-k", "Rule paused (30.0s)", 30.0)) is True

    row = job_row(store, "job-k")
    assert row["attempts"] == 0
    assert row["status"] == "RETRY_WAIT"
    assert row["next_retry_at"] >= before + 29.0
    assert row["lease_until"] == 0
    assert row["error_detail"] == "Rule paused (30.0s)"


def test_defer_never_reopens_a_job_that_already_finished(store):
    """A late deferral must not resurrect a SENT or FAILED job."""
    seed(store, make_rule())
    enqueue(store, make_job())
    queue = SqliteDeliveryQueueRepository(store)
    run(queue.mark_sent("job-k", 1))

    assert run(queue.defer("job-k", "Rule paused", 30.0)) is False
    assert job_row(store, "job-k")["status"] == "SENT"


def test_mark_retry_still_counts_a_genuine_delivery_failure(store):
    """The distinction is real: a Telegram refusal still consumes an attempt."""
    seed(store, make_rule())
    enqueue(store, make_job())
    queue = SqliteDeliveryQueueRepository(store)

    run(queue.mark_retry("job-k", "FLOOD_WAIT", 5.0))
    row = job_row(store, "job-k")
    assert row["attempts"] == 1
    assert row["status"] == "RETRY_WAIT"


def test_the_queue_falls_back_when_an_adapter_cannot_defer(store):
    """An adapter without defer() keeps the old behaviour, and says so."""
    seed(store, make_rule(is_paused=True))
    enqueue(store, make_job())
    worker = queue_for(store, FakePool(FakeClient()))

    async def no_defer(*_a, **_k):
        return False

    worker._queue.defer = no_defer
    run(worker._process_job("w1", make_job()))

    row = job_row(store, "job-k")
    assert row["attempts"] == 1, "the fallback must still postpone the job"


def test_the_postgres_defer_does_not_touch_attempts():
    """No Postgres server in the unit run: assert the statement's shape."""
    import inspect
    import re

    from core.infrastructure.persistence.postgres_repositories import (
        PostgresDeliveryQueueRepository,
    )

    src = inspect.getsource(PostgresDeliveryQueueRepository.defer)
    statement = src[src.index("UPDATE delivery_jobs"):]
    assert "attempts" not in statement.split("WHERE")[0], (
        "the deferral must not write attempts: it is not a failed attempt"
    )
    assert "RETRY_WAIT" in statement
    assert "NOT IN ('SENT', 'FAILED')" in statement.replace("%s", "").replace("  ", " ")
    assert re.search(r"next_retry_at", statement)
