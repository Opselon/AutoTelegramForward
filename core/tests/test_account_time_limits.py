"""Account-level restrictions must be one shared, durable, honest state.

Three defects motivated this file:

* the suspension lived in a process-local dict, so a restart (or a second
  replica) saw a free account and kept sending — Telegram answers that by
  extending the wait;
* every "recoverable" error called ``set_time_limit`` with the *jittered
  backoff* as the duration, so a connection reset suspended a whole account for
  a made-up period;
* an incoming message during a suspension was **dropped** by the ingest
  pipeline: "suspended" must mean "queued until it ends", not "lost".
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from pyrogram.errors import ChatWriteForbidden, FloodWait

from core.domain.entities import DeliveryJob, ForwardRule, MessagePayload, TelegramSession
from core.domain.value_objects import MediaType
from core.infrastructure.persistence.account_limits import AccountLimitStore
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryQueueRepository,
    SqliteForwardRuleRepository,
    SqliteFilterRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.client_pool import ClientPool
from core.infrastructure.telegram.durable_queue import DurableQueueManager
from core.infrastructure.telegram.failure_policy import rate_limit_seconds

OWNER = 5094837833
SOURCE = "-1004400615270"
TARGET = "-1003915313919"


class FloodingClient:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def get_chat(self, chat_id):
        from types import SimpleNamespace
        return SimpleNamespace(id=chat_id)

    def __getattr__(self, name: str):
        async def _raise(*args, **kwargs):
            self.calls += 1
            raise self.error

        return _raise


class FakePool:
    """Pool double for the worker: a real ClientPool is used in the pool tests."""

    def __init__(self, client, limited: bool = False, remaining: float = 0.0) -> None:
        self.client = client
        self.limited = limited
        self.remaining = remaining
        self.limits: list[tuple] = []

    def get(self, session_id: str):
        return self.client

    def is_time_limited(self, session_id: str) -> bool:
        return self.limited

    def remaining_limit_seconds(self, session_id: str) -> float:
        return self.remaining

    def set_time_limit(self, session_id: str, seconds: float, **kw) -> None:
        self.limits.append((session_id, seconds, kw))
        self.limited = True
        self.remaining = seconds


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text,
                               evaluation=None, job_kind="forward",
                               correlation_id="", branding=None):
        self.jobs.append((rule.id, payload.message_id, str(target_chat_id), text))
        return True


def make_rule(rule_id: str = "rule-l", **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id, session_id="sess-l", source_chat_id=SOURCE,
        target_chat_id=TARGET, is_active=True, owner_user_id=OWNER, **kwargs,
    )
    rule.duplicate_filters = False
    return rule


def make_job(job_id: str = "job-l", mid: int = 1) -> DeliveryJob:
    return DeliveryJob(
        id=job_id, rule_id="rule-l", source_chat_id=SOURCE, source_message_id=mid,
        target_chat_id=TARGET,
        payload_data={"session_id": "sess-l", "text": "hi", "delivery_transport": "USER"},
        max_attempts=5,
    )


@pytest.fixture
def store(tmp_path):
    db = SqliteDatabase(os.path.join(tmp_path, "limits.db"))
    try:
        yield db
    finally:
        db.close()


def seed(db, *rules) -> None:
    async def go():
        await SqliteSessionRepository(db, CryptoService("test-key")).add(TelegramSession(
            id="sess-l", phone_number="+100****0000", is_active=True,
            is_authorized=True, owner_user_id=OWNER,
        ))
        repo = SqliteForwardRuleRepository(db)
        for rule in rules:
            await repo.add(rule)

    asyncio.run(go())


def worker(db, pool, notifier=None) -> DurableQueueManager:
    return DurableQueueManager(
        queue_repo=SqliteDeliveryQueueRepository(db),
        message_map_repo=SqliteMessageMapRepository(db),
        client_pool=pool,
        rule_repo=SqliteForwardRuleRepository(db),
        num_workers=1,
        owner_notifier=notifier,
    )


# ---------------------------------------------------------------------------
# Only a real rate-limit error is a rate limit
# ---------------------------------------------------------------------------

def test_only_flood_errors_carry_a_wait():
    assert rate_limit_seconds(FloodWait(value=300)) == 300
    assert rate_limit_seconds(ChatWriteForbidden()) == 0
    assert rate_limit_seconds(ConnectionError("RPC_CALL_FAIL")) == 0


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def test_store_round_trips_and_is_monotonic(store):
    limits = AccountLimitStore(store)
    until = time.time() + 120
    limits.set("sess-l", until, code="FLOOD_WAIT_X", reason="t")
    stored = limits.get("sess-l")
    assert stored is not None and stored.until == pytest.approx(until)
    assert stored.code == "FLOOD_WAIT_X"

    # A shorter, later wait must not shorten the current suspension.
    limits.set("sess-l", until - 60, code="FLOOD_WAIT_X")
    stored = limits.get("sess-l")
    assert stored is not None and stored.until == pytest.approx(until)

    assert [l.session_id for l in limits.list_active()] == ["sess-l"]
    limits.clear("sess-l")
    assert limits.get("sess-l") is None


def test_expired_rows_are_purged(store):
    limits = AccountLimitStore(store)
    limits.set("sess-old", time.time() - 5, code="FLOOD_WAIT_X")
    assert limits.purge_expired() == 1
    assert limits.get("sess-old") is None


# ---------------------------------------------------------------------------
# The pool: shared state, survives a "restart"
# ---------------------------------------------------------------------------

def test_a_suspension_survives_a_restart(store):
    first = ClientPool(limit_store=AccountLimitStore(store))
    first.set_time_limit("sess-l", 300, code="FLOOD_WAIT_X")
    assert first.is_time_limited("sess-l")

    # A new process (same database) must not see a free account.
    restarted = ClientPool(limit_store=AccountLimitStore(store))
    assert restarted.hydrate_limits() == 1
    assert restarted.is_time_limited("sess-l")
    assert restarted.remaining_limit_seconds("sess-l") > 250


def test_expiry_clears_memory_and_the_row(store):
    limits = AccountLimitStore(store)
    limits.set("sess-l", time.time() - 1, code="FLOOD_WAIT_X")
    pool = ClientPool(limit_store=limits)
    assert pool.is_time_limited("sess-l") is False
    assert limits.get("sess-l") is None


def test_a_shorter_wait_does_not_shorten_a_suspension(store):
    pool = ClientPool(limit_store=AccountLimitStore(store))
    pool.set_time_limit("sess-l", 300, code="FLOOD_WAIT_X")
    before = pool.remaining_limit_seconds("sess-l")
    pool.set_time_limit("sess-l", 10, code="FLOOD_WAIT_X")
    assert pool.remaining_limit_seconds("sess-l") == pytest.approx(before, abs=1.0)


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

def test_flood_wait_suspends_for_telegrams_duration_and_notifies_once(store):
    seed(store, make_rule())
    asyncio.run(SqliteDeliveryQueueRepository(store).enqueue(make_job("job-1", 1)))
    asyncio.run(SqliteDeliveryQueueRepository(store).enqueue(make_job("job-2", 2)))

    notes: list[dict] = []
    pool = FakePool(FloodingClient(FloodWait(value=300)))
    mgr = worker(store, pool, lambda owner, rule, payload: notes.append(payload))

    asyncio.run(mgr._process_job("w1", make_job("job-1", 1)))
    asyncio.run(mgr._process_job("w1", make_job("job-2", 2)))

    assert pool.limits, "a FloodWait must suspend the account"
    _, seconds, kwargs = pool.limits[0]
    assert seconds == 300, f"the suspension must use Telegram's duration, got {seconds}"
    assert kwargs.get("code") == "FLOOD_WAIT_X"

    kinds = [n["kind"] for n in notes]
    assert kinds.count("account_restricted") == 1, f"one notice per suspension: {kinds}"
    assert notes[0]["minutes"] == 5


def test_a_recoverable_network_error_does_not_suspend_the_account(store):
    seed(store, make_rule())
    asyncio.run(SqliteDeliveryQueueRepository(store).enqueue(make_job()))

    pool = FakePool(FloodingClient(ConnectionError("RPC_CALL_FAIL")))
    mgr = worker(store, pool)
    asyncio.run(mgr._process_job("w1", make_job()))

    assert pool.limits == [], "a network error is not a rate limit"


# ---------------------------------------------------------------------------
# The ingest side: queued, not dropped
# ---------------------------------------------------------------------------

def test_messages_arriving_during_a_suspension_are_queued(tmp_path):
    from core.application.pipeline import DurableMessagePipeline

    db = SqliteDatabase(os.path.join(tmp_path, "ingest.db"))
    queue = RecordingQueue()
    rule = make_rule()
    seed(db, rule)
    pipe = DurableMessagePipeline(
        rule_repo=SqliteForwardRuleRepository(db),
        filter_repo=SqliteFilterRuleRepository(db),
        queue_manager=queue,
        msg_map_repo=SqliteMessageMapRepository(db),
        processed_repo=SqliteProcessedMessageRepository(db),
        pool=FakePool(None, limited=True, remaining=120.0),
    )
    payload = MessagePayload(message_id=5, chat_id=SOURCE, text="during the wait",
                             media_type=MediaType.TEXT)
    asyncio.run(pipe.process(payload, trigger="NEW_MESSAGE"))
    assert queue.jobs, "a message during a suspension must be queued, not dropped"
