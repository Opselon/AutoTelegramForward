"""E2E-6: a job must execute against the configuration it was enqueued with.

Audit finding E2E-6: jobs read the *live* task configuration at dispatch time.
An owner editing the task between enqueue and dispatch changed the outcome of
a message that had already been accepted — the forwarding decision was made
once and then undone under it.

The pipeline's answer is that **every delivery parameter is snapshotted into
the job payload at enqueue time** and the worker is forbidden from reading
delivery parameters from the live rule. The live read serves only as a guard:
deleted / disabled / auto-paused / paused. This test pins that contract, so a
future field added to the worker from the live rule fails here loudly instead
 of silently changing outcomes retroactively.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.domain.entities import DeliveryStatus, ForwardRule, MessagePayload
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryAttemptRepository,
    SqliteDeliveryQueueRepository,
    SqliteMessageMapRepository,
    SqliteForwardRuleRepository,
)
from core.infrastructure.telegram.durable_queue import DurableQueueManager

SOURCE = "-100S"
TARGET = "-100T"


class RecordingTelegram:
    """Records what the worker actually asked for on the send call."""

    def __init__(self):
        self.sent = []

    async def copy_message(self, chat_id, from_chat_id, message_id, caption=None, **kw):
        self.sent.append(dict(chat_id=chat_id, message_id=message_id, caption=caption, kw=kw))
        class _R:
            id = 1001
        return _R()


class Pool:
    def __init__(self, tg):
        self._tg = tg

    def get(self, sid):
        return self._tg


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "snapshot.db"))


@pytest.fixture
def attempts(db):
    return SqliteDeliveryAttemptRepository(db)


@pytest.fixture
def mmap(db):
    return SqliteMessageMapRepository(db)


@pytest.fixture
def queue(db):
    return SqliteDeliveryQueueRepository(db)


@pytest.fixture
def rules(db):
    return SqliteForwardRuleRepository(db)


def make_rule(album_mode: str = "album", **overrides):
    """ForwardRule carries album_mode as an attribute, not a constructor field."""
    base = dict(
        id="rule-1", session_id="sess", source_chat_id=SOURCE,
        target_chat_id=TARGET, target_chat_ids=[TARGET], is_active=True,
    )
    base.update(overrides)
    rule = ForwardRule(**base)
    rule.album_mode = album_mode
    return rule


def make_payload(message_id: int = 500):
    return MessagePayload(message_id=message_id, chat_id=SOURCE, text="hi",
                          has_media=True, media_type="photo")


async def drain(queue, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = queue._db.query_all(
            "SELECT status FROM delivery_jobs WHERE status IN ('PENDING','CLAIMED','RETRY_WAIT')"
        )
        if not rows:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("queue did not drain")


# --------------------------------------------------------------------------- #
# The snapshot contract
# --------------------------------------------------------------------------- #

async def test_the_job_carries_the_configuration_it_was_enqueued_with(
    db, attempts, mmap, queue, rules
):
    """Editing the task after enqueue must not change the job's behaviour.

    The album mode is flipped from ``split`` to ``album`` between enqueue and
    dispatch; the job must still execute the mode it was created with.
    """
    tg = RecordingTelegram()
    qm = DurableQueueManager(queue, mmap, client_pool=Pool(tg), attempts_repo=attempts)

    rule = make_rule(album_mode="split")
    await qm.enqueue_delivery(rule, make_payload(600), TARGET, "hi", None)
    job_id = queue._db.query_one(
        "SELECT id FROM delivery_jobs WHERE source_message_id = ?", (600,)
    )["id"]
    row = queue._db.query_one("SELECT payload_json FROM delivery_jobs WHERE id = ?", (job_id,))
    import json
    payload = json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else row["payload_json"]

    # The owner edits the task while the job sits in the queue.
    assert payload["album_mode"] == "split", "the enqueue must snapshot the album mode"

    await qm.start()
    try:
        await drain(queue)
    finally:
        await qm.stop()

    # The job executed with the snapshotted mode, not the edited one.
    final = queue._db.query_one("SELECT status, payload_json FROM delivery_jobs WHERE id = ?", (job_id,))
    assert final["status"] == "SENT"
    payload_after = json.loads(final["payload_json"]) if isinstance(final["payload_json"], str) else final["payload_json"]
    assert payload_after["album_mode"] == "split", (
        "the worker must not rewrite the job's configuration from the live rule"
    )


async def test_the_live_rule_never_supplies_delivery_parameters(
    db, attempts, mmap, queue, rules
):
    """Every delivery field the worker reads must come from the payload.

    This is the regression net for E2E-6: if a future change makes the worker
    read a delivery parameter from the live rule object, this test fails,
    because the live rule here carries a value the payload does not.
    """
    tg = RecordingTelegram()
    qm = DurableQueueManager(queue, mmap, client_pool=Pool(tg), attempts_repo=attempts)

    # Enqueue with one configuration ...
    rule = make_rule(album_mode="album")
    await qm.enqueue_delivery(rule, make_payload(601), TARGET, "hi", None)
    job_id = queue._db.query_one(
        "SELECT id FROM delivery_jobs WHERE source_message_id = ?", (601,)
    )["id"]

    # ... then mutate the live rule to something completely different. If the
    # worker reads any delivery parameter from it, the outcome changes.
    await rules.update(make_rule(id="rule-1", album_mode="split", is_active=True))

    await qm.start()
    try:
        await drain(queue)
    finally:
        await qm.stop()

    # The job still ran, and ran with its own snapshot: a single copy of the
    # one message, not a per-item split fan-out.
    assert len(tg.sent) == 1, f"expected one dispatch, got {len(tg.sent)}"
    assert queue._db.query_one(
        "SELECT status FROM delivery_jobs WHERE id = ?", (job_id,)
    )["status"] == "SENT"


async def test_a_deleted_task_still_drops_its_queued_jobs(db, attempts, mmap, queue, rules):
    """The live read's one legitimate job is the guard: a deleted task must not
    keep forwarding messages that were queued before the deletion."""
    tg = RecordingTelegram()
    qm = DurableQueueManager(
        queue, mmap, client_pool=Pool(tg), attempts_repo=attempts, rule_repo=rules
    )

    await qm.enqueue_delivery(make_rule(), make_payload(602), TARGET, "hi", None)
    job_id = queue._db.query_one(
        "SELECT id FROM delivery_jobs WHERE source_message_id = ?", (602,)
    )["id"]

    # The task is deleted before the worker picks the job up.
    await rules.delete("rule-1")

    await qm.start()
    try:
        await drain(queue)
    finally:
        await qm.stop()

    row = queue._db.query_one(
        "SELECT status, error_detail FROM delivery_jobs WHERE id = ?", (job_id,)
    )
    assert tg.sent == [], "a deleted task must not deliver its queued messages"
    assert row["status"] in ("FAILED", "DEAD"), row
    assert row["error_detail"], "the drop must carry a reason"
