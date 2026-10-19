"""An invalid source/target must pause *that* task, visibly, with a way back.

Measured on the pre-change code: a permanent delivery error (`CHAT_WRITE_FORBIDDEN`,
`CHANNEL_INVALID`, a banned target) set `is_active = False` — indistinguishable
from the owner switching the task off, no reason stored, no notification, and no
recovery except re-enabling a task that would fail again. The same branch also
treated every `CRITICAL` error as permanent, so a network blip paused tasks.

These tests pin the replacement, against the real SQLite store and real pyrogram
error classes (never a live Telegram call):

* a dead chat pauses only the owning task (`is_paused` + `auto_pause` metadata)
  and leaves every other task alone;
* the owner is told, once, with the failing chat and the code;
* queued work for the paused task is failed with the reason instead of being
  retried forever or delivered into the void;
* a transient error never pauses anything.
"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest
from pyrogram.errors import ChannelInvalid, ChatWriteForbidden

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
from core.infrastructure.telegram.failure_policy import classify_failure

OWNER = 5094837833
OTHER_OWNER = 6278059256
SOURCE = "-1004400615270"
TARGET = "-1003915313919"


class FailingClient:
    """A client whose sends always raise ``error``; ``get_chat`` may fail too."""

    def __init__(self, error: BaseException, *, chats_ok: bool = True) -> None:
        self.error = error
        self.chats_ok = chats_ok
        self.sent: list[tuple[str, dict]] = []

    async def get_chat(self, chat_id):
        if not self.chats_ok:
            raise ChannelInvalid()
        return SimpleNamespace(id=chat_id)

    def __getattr__(self, name: str):
        async def _raise(*args, **kwargs):
            self.sent.append((name, kwargs))
            raise self.error

        return _raise


class FakePool:
    def __init__(self, client) -> None:
        self.client = client

    def get(self, session_id: str):
        return self.client

    def is_time_limited(self, session_id: str) -> bool:
        return False


def make_rule(rule_id: str = "rule-a", owner: int = OWNER, **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id,
        session_id="sess-a",
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        is_active=True,
        owner_user_id=owner,
        **kwargs,
    )
    rule.duplicate_filters = False
    return rule


def make_job(rule_id: str = "rule-a", job_id: str = "job-a") -> DeliveryJob:
    return DeliveryJob(
        id=job_id,
        rule_id=rule_id,
        source_chat_id=SOURCE,
        source_message_id=901,
        target_chat_id=TARGET,
        payload_data={
            "session_id": "sess-a",
            "text": "hello",
            "delivery_transport": "USER",
        },
        max_attempts=5,
    )


def queue_for(db, pool, notifier=None) -> DurableQueueManager:
    return DurableQueueManager(
        queue_repo=SqliteDeliveryQueueRepository(db),
        message_map_repo=SqliteMessageMapRepository(db),
        client_pool=pool,
        rule_repo=SqliteForwardRuleRepository(db),
        num_workers=1,
        owner_notifier=notifier,
    )


def seed(db, *rules) -> None:
    async def go():
        await SqliteSessionRepository(db, CryptoService("test-key")).add(TelegramSession(
            id="sess-a", phone_number="+100****0000", is_active=True,
            is_authorized=True, owner_user_id=OWNER,
        ))
        repo = SqliteForwardRuleRepository(db)
        for rule in rules:
            await repo.add(rule)

    asyncio.run(go())


def job_row(db, job_id: str = "job-a") -> dict:
    return dict(db.query_one("SELECT * FROM delivery_jobs WHERE id = ?", (job_id,)))


def stored_rule(db, rule_id: str = "rule-a") -> ForwardRule:
    return asyncio.run(SqliteForwardRuleRepository(db).get_by_id(rule_id))


@pytest.fixture
def store(tmp_path):
    db = SqliteDatabase(os.path.join(tmp_path, "autopause.db"))
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_real_telegram_errors_classify_as_expected():
    assert classify_failure(ChatWriteForbidden()).kind == "permanent"
    assert classify_failure(ChannelInvalid()).kind == "permanent"
    assert classify_failure(ConnectionError("RPC_CALL_FAIL")).kind == "transient"
    assert classify_failure(asyncio.TimeoutError()).kind == "transient"


def test_a_network_error_is_not_permanent():
    decision = classify_failure(ConnectionError("connection reset by peer"))
    assert decision.kind != "permanent"
    assert decision.pauses_task is False


# ---------------------------------------------------------------------------
# The task model
# ---------------------------------------------------------------------------

def test_mark_auto_paused_persists_reason_and_is_idempotent():
    rule = make_rule()
    assert rule.mark_auto_paused("CHAT_WRITE_FORBIDDEN", "no rights", chat_id=TARGET,
                                scope="target", detail="403 CHAT_WRITE_FORBIDDEN")
    assert rule.is_paused and rule.is_currently_paused()
    assert rule.auto_pause["code"] == "CHAT_WRITE_FORBIDDEN"
    assert rule.auto_pause["chat_id"] == TARGET
    assert rule.auto_pause["scope"] == "target"
    # Repeating the same failure must not rewrite the rule (one notification,
    # not one per queued job for the same dead chat).
    assert rule.mark_auto_paused("CHAT_WRITE_FORBIDDEN", "no rights", chat_id=TARGET) is False


def test_clearing_restores_the_owners_previous_pause_state():
    paused_by_owner = make_rule(is_paused=True, paused_until=0)
    paused_by_owner.mark_auto_paused("CHANNEL_INVALID", "gone", chat_id=TARGET)
    paused_by_owner.clear_auto_pause()
    assert paused_by_owner.auto_pause == {}
    assert paused_by_owner.is_paused is True, "the owner's own pause must survive recovery"

    running = make_rule()
    running.mark_auto_paused("CHANNEL_INVALID", "gone", chat_id=TARGET)
    running.clear_auto_pause()
    assert running.is_paused is False
    assert running.is_currently_paused() is False


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

def test_permanent_failure_pauses_only_the_owning_task_and_notifies(store):
    seed(store, make_rule(), make_rule(rule_id="rule-other", owner=OTHER_OWNER))
    enqueue = SqliteDeliveryQueueRepository(store).enqueue
    asyncio.run(enqueue(make_job()))

    notes: list[tuple] = []

    def notifier(owner, rule, payload):
        notes.append((owner, rule.id, dict(payload)))

    client = FailingClient(ChatWriteForbidden())
    worker = queue_for(store, FakePool(client), notifier)
    result = asyncio.run(worker._process_job("w1", make_job()))

    assert result is None
    assert job_row(store)["status"] == "FAILED"
    paused = stored_rule(store)
    assert paused.is_paused and paused.is_currently_paused()
    assert paused.is_active is True, "a pause is not the owner's on/off switch"
    assert paused.auto_pause["code"] == "CHAT_WRITE_FORBIDDEN"
    assert paused.auto_pause["chat_id"] == TARGET
    assert paused.auto_pause["scope"] == "target"

    assert len(notes) == 1, f"the owner must be told exactly once: {notes}"
    owner, rule_id, payload = notes[0]
    assert owner == OWNER and rule_id == "rule-a"
    assert payload["kind"] == "task_paused"
    assert payload["chat_id"] == TARGET

    # The other tenant's task is untouched.
    other = stored_rule(store, "rule-other")
    assert other.is_paused is False and other.auto_pause == {}


def test_source_side_failure_is_reported_as_the_source(store):
    """If the target resolves but the source does not, blame the source."""
    seed(store, make_rule())
    asyncio.run(SqliteDeliveryQueueRepository(store).enqueue(make_job()))

    class TargetOkSourceGone(FailingClient):
        async def get_chat(self, chat_id):
            if str(chat_id) == SOURCE:
                raise ChannelInvalid()
            return SimpleNamespace(id=chat_id)

    client = TargetOkSourceGone(ChatWriteForbidden())
    worker = queue_for(store, FakePool(client), lambda *a: None)
    asyncio.run(worker._process_job("w1", make_job()))

    paused = stored_rule(store)
    assert paused.auto_pause["scope"] == "source"
    assert paused.auto_pause["chat_id"] == SOURCE


def test_a_transient_error_does_not_pause_the_task(store):
    seed(store, make_rule())
    asyncio.run(SqliteDeliveryQueueRepository(store).enqueue(make_job()))

    worker = queue_for(store, FakePool(FailingClient(ConnectionError("RPC_CALL_FAIL"))),
                       lambda *a: None)
    asyncio.run(worker._process_job("w1", make_job()))

    fresh = stored_rule(store)
    assert fresh.is_paused is False and fresh.auto_pause == {}
    assert job_row(store)["status"] in ("PENDING", "RETRY_WAIT")


def test_queued_work_for_an_auto_paused_task_is_failed_with_the_reason(store):
    rule = make_rule()
    rule.mark_auto_paused("CHANNEL_INVALID", "CHANNEL_INVALID", chat_id=TARGET, scope="target")
    seed(store, rule)
    asyncio.run(SqliteDeliveryQueueRepository(store).enqueue(make_job()))

    client = FailingClient(ChatWriteForbidden())
    worker = queue_for(store, FakePool(client), lambda *a: None)
    asyncio.run(worker._process_job("w1", make_job()))

    assert client.sent == [], "nothing may be sent for a paused task"
    row = job_row(store)
    assert row["status"] == "FAILED"
    assert "paused" in str(row["error_detail"]).lower()


def test_without_a_notifier_the_pause_still_happens(store):
    seed(store, make_rule())
    asyncio.run(SqliteDeliveryQueueRepository(store).enqueue(make_job()))
    worker = queue_for(store, FakePool(FailingClient(ChatWriteForbidden())))
    asyncio.run(worker._process_job("w1", make_job()))
    assert stored_rule(store).auto_pause["code"] == "CHAT_WRITE_FORBIDDEN"
