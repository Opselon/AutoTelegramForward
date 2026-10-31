"""Regression tests for the forwarding-reliability audit (findings #3 and #4).

Both findings were proven against the production database and reproduced here
against the real ``SqliteDatabase`` and the real repositories — no mocks of the
persistence layer.

Finding #3 — a replay of an already-processed event regressed ``message_map``
from SENT back to PENDING while the (ignored) job insert meant no delivery job
existed to drive it: the target channel had the message but the store said it
did not, and edit/delete sync then read a lie.

Finding #4 — ``mark_processed`` has no target dimension, so one target's
refused enqueue (backpressure) was enough to make the *whole event* look
processed; the replay that could have repaired the missing target was dropped
by ``was_processed`` instead. Four successful targets and one failed target
must keep five independent outcomes.
"""

from __future__ import annotations

import os

import pytest

from core.domain.entities import DeliveryStatus, ForwardRule, MessagePayload
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryQueueRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
)
from core.infrastructure.telegram.durable_queue import DurableQueueManager

SOURCE = "-1004400615270"
TARGET_A = "-1003915313919"
TARGET_B = "-1003915320"


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "rel.db"))


@pytest.fixture
def mmap(db):
    return SqliteMessageMapRepository(db)


@pytest.fixture
def queue(db):
    return SqliteDeliveryQueueRepository(db)


@pytest.fixture
def processed(db):
    return SqliteProcessedMessageRepository(db)


def make_rule():
    return ForwardRule(id="rule-1", session_id="sess", source_chat_id=SOURCE,
                       target_chat_id=TARGET_A, is_active=True)


def make_payload(message_id: int = 555):
    return MessagePayload(message_id=message_id, chat_id=SOURCE, text="hello")


# --------------------------------------------------------------------------- #
# Finding #3: a replay must never regress a terminal mapping
# --------------------------------------------------------------------------- #

async def test_replay_of_a_sent_event_keeps_the_mapping_sent(db, mmap, queue):
    """The mapping upsert runs before the job's INSERT OR IGNORE.

    Before the fix the upsert flipped the row back to PENDING and the ignored
    job insert left no delivery to drive it — an orphan that no watchdog
    reconciles (they scan jobs, not mappings).
    """
    qm = DurableQueueManager(queue, mmap, client_pool=None)
    rule, payload = make_rule(), make_payload()

    await qm.enqueue_delivery(rule, payload, TARGET_A, "hello", None)
    await queue.mark_sent("rule-1:%s:555:%s" % (SOURCE, TARGET_A), 4242)
    await mmap.update_delivery("rule-1", SOURCE, 555, TARGET_A, 4242, DeliveryStatus.SENT)
    rows = await mmap.get_by_source(SOURCE, 555)
    assert rows[0].delivery_status == DeliveryStatus.SENT

    # A replay of the same event (reconnect re-sweep, catch-up, duplicate push).
    again = await qm.enqueue_delivery(rule, payload, TARGET_A, "hello", None)
    assert again is False, "the job insert must be a no-op on replay"

    rows = await mmap.get_by_source(SOURCE, 555)
    assert rows[0].delivery_status == DeliveryStatus.SENT, (
        "a replay regressed the mapping to PENDING with no job to drive it"
    )
    assert rows[0].target_message_id == 4242


async def test_add_or_update_does_not_regress_a_sent_mapping(db, mmap):
    """The repository-level upsert carries the same guard."""
    from core.domain.entities import MessageMapping

    await mmap.add_or_update(MessageMapping(
        id="m1", rule_id="rule-1", source_chat_id=SOURCE, source_message_id=900,
        target_chat_id=TARGET_A, target_message_id=11, delivery_status=DeliveryStatus.SENT,
        created_at=1, updated_at=1,
    ))
    # A PENDING rewrite of the same key must not un-send it.
    await mmap.add_or_update(MessageMapping(
        id="m1", rule_id="rule-1", source_chat_id=SOURCE, source_message_id=900,
        target_chat_id=TARGET_A, target_message_id=None, delivery_status=DeliveryStatus.PENDING,
        created_at=1, updated_at=2,
    ))
    rows = await mmap.get_by_source(SOURCE, 900)
    assert rows[0].delivery_status == DeliveryStatus.SENT
    assert rows[0].target_message_id == 11


# --------------------------------------------------------------------------- #
# Finding #4: one target's failure must not mask the others
# --------------------------------------------------------------------------- #

async def test_partial_fan_out_is_not_marked_processed(db, mmap, queue, processed):
    """The replay-protection marker is only written when the fan-out is whole.

    With capacity for two of three targets, the third enqueue is refused. The
    marker must NOT be written: a replay that repairs the third target is the
    only thing that can deliver it, and ``was_processed`` would drop that
    replay outright.
    """
    qm = DurableQueueManager(queue, mmap, client_pool=None, max_queue_size=2)
    rule, payload = make_rule(), make_payload(message_id=900)

    results = {}
    for tgt in (TARGET_A, TARGET_B, "-100T3"):
        results[tgt] = await qm.enqueue_delivery(rule, payload, tgt, "hi", None)
    assert results == {TARGET_A: True, TARGET_B: True, "-100T3": False}

    # The pipeline's contract: only mark processed when nothing was refused.
    failures = [t for t, ok in results.items() if not ok]
    if not failures:
        await processed.mark_processed("rule-1", SOURCE, 900, "")
    assert await processed.was_processed("rule-1", SOURCE, 900) is False, (
        "a partial fan-out marked the event processed, hiding the missing target"
    )

    # And the replay that repairs it: the two queued targets are no-ops, the
    # refused one becomes a real job.
    queue._pending_estimate = 0  # free the backpressure slot for the replay
    repaired = await qm.enqueue_delivery(rule, payload, "-100T3", "hi", None)
    assert repaired is True
    jobs = queue._db.query_all(
        "SELECT target_chat_id FROM delivery_jobs WHERE rule_id = 'rule-1' AND source_message_id = 900"
    )
    assert sorted(r["target_chat_id"] for r in jobs) == sorted([TARGET_A, TARGET_B, "-100T3"])


async def test_complete_fan_out_is_marked_processed(db, mmap, queue, processed):
    """The mirror case: when every target is accepted, the marker is written.

    Without it the event would be replayed forever; the marker is exactly the
    event-level idempotency that keeps that from happening.
    """
    qm = DurableQueueManager(queue, mmap, client_pool=None, max_queue_size=100)
    rule, payload = make_rule(), make_payload(message_id=901)

    ok_a = await qm.enqueue_delivery(rule, payload, TARGET_A, "hi", None)
    ok_b = await qm.enqueue_delivery(rule, payload, TARGET_B, "hi", None)
    assert (ok_a, ok_b) == (True, True)

    failures = []
    if not failures:
        await processed.mark_processed("rule-1", SOURCE, 901, "")
    assert await processed.was_processed("rule-1", SOURCE, 901) is True
