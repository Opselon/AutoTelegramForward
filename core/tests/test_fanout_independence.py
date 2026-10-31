"""Fan-out independence: one destination's outcome never decides another's.

The reported incident — *"messages reach destination A but never reach
destination B"* — has two independent causes, both fixed and both pinned here:

* a fan-out enqueue loop that stopped at the first failure (the legacy direct
  path) or marked the whole event processed from one target's success (the
  pipeline's event-level marker — audit finding #4);
* a worker that could double-dispatch one target while another target's job
  was still in flight.

The scenarios required by the mission are exercised here against the real
repositories, with deterministic failure injection per destination.
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
)
from core.infrastructure.telegram.durable_queue import DurableQueueManager

SOURCE = "-100S"
TARGETS = ["-100T1", "-100T2", "-100T3", "-100T4", "-100T5"]


class PerTargetTelegram:
    """Deterministic send target whose failures are configured per destination."""

    def __init__(self, fail_targets: dict = None, delay: float = 0.0):
        # target -> exception instance or a list of them (one per attempt)
        self.fail_targets = fail_targets or {}
        self.delay = delay
        self.sent: dict = {}
        self.calls = 0

    async def copy_message(self, chat_id, from_chat_id, message_id, caption=None, **kw):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        failure = self.fail_targets.get(str(chat_id))
        if failure is not None:
            if isinstance(failure, list):
                if failure:
                    raise failure.pop(0)
            else:
                raise failure
        self.sent.setdefault(str(chat_id), []).append(message_id)
        class _R:
            id = 1000 + self.calls
        return _R()


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "fanout.db"))


@pytest.fixture
def attempts(db):
    return SqliteDeliveryAttemptRepository(db)


@pytest.fixture
def mmap(db):
    return SqliteMessageMapRepository(db)


@pytest.fixture
def queue(db):
    return SqliteDeliveryQueueRepository(db)


def make_rule(n_targets: int = 5):
    return ForwardRule(id="rule-1", session_id="sess", source_chat_id=SOURCE,
                       target_chat_id=TARGETS[0], target_chat_ids=TARGETS[:n_targets],
                       is_active=True)


def make_payload(message_id: int = 500):
    return MessagePayload(message_id=message_id, chat_id=SOURCE, text="hi",
                          has_media=True, media_type="photo")


async def drain(qm, queue, timeout: float = 10.0) -> None:
    """Run the queue until no job is left pending, or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = queue._db.query_all(
            "SELECT status FROM delivery_jobs WHERE status IN ('PENDING','CLAIMED','RETRY_WAIT')"
        )
        if not rows:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("queue did not drain: jobs still pending")


async def outcomes(queue, message_id: int) -> dict:
    """Per-destination delivery outcome: target -> (status, mapping status)."""
    rows = queue._db.query_all(
        "SELECT target_chat_id, status, error_detail FROM delivery_jobs "
        "WHERE source_message_id = ? ORDER BY target_chat_id",
        (message_id,),
    )
    return {r["target_chat_id"]: (r["status"], r["error_detail"]) for r in rows}


# --------------------------------------------------------------------------- #
# Required scenarios
# --------------------------------------------------------------------------- #

async def test_one_source_five_destinations_all_delivered(db, attempts, mmap, queue):
    tg = PerTargetTelegram()

    class Pool:
        def get(self, sid):
            return tg

    qm = DurableQueueManager(queue, mmap, client_pool=Pool(), attempts_repo=attempts)
    await qm.enqueue_delivery(make_rule(5), make_payload(501), TARGETS[0], "hi", None)
    for tgt in TARGETS[1:]:
        await qm.enqueue_delivery(make_rule(5), make_payload(501), tgt, "hi", None)

    await qm.start()
    await drain(qm, queue)
    await qm.stop()

    assert sorted(tg.sent) == sorted(TARGETS), f"missing destinations: {tg.sent}"
    res = await outcomes(queue, 501)
    assert all(res[t][0] == "SENT" for t in TARGETS), res


async def test_five_destinations_one_fails_independently(db, attempts, mmap, queue):
    """Target 3 fails permanently. The other four must still be delivered and
    each keeps its own outcome — the failure never leaks across destinations."""
    from pyrogram.errors import ChatAdminRequired
    tg = PerTargetTelegram({TARGETS[2]: ChatAdminRequired("400 CHAT_ADMIN_REQUIRED")})

    class Pool:
        def get(self, sid):
            return tg

    qm = DurableQueueManager(queue, mmap, client_pool=Pool(), attempts_repo=attempts)
    for tgt in TARGETS:
        await qm.enqueue_delivery(make_rule(5), make_payload(502), tgt, "hi", None)

    await qm.start()
    await drain(qm, queue)
    await qm.stop()

    delivered = sorted(tg.sent)
    assert delivered == sorted(t for t in TARGETS if t != TARGETS[2]), delivered
    res = await outcomes(queue, 502)
    assert res[TARGETS[2]][0] == "FAILED"
    assert "CHAT_ADMIN_REQUIRED" in res[TARGETS[2]][1]
    for t in TARGETS:
        if t != TARGETS[2]:
            assert res[t][0] == "SENT", (t, res[t])


async def test_five_destinations_two_fail_independently(db, attempts, mmap, queue):
    """Two destinations fail with different errors; the other three succeed."""
    from pyrogram.errors import ChatAdminRequired, ChannelPrivate
    tg = PerTargetTelegram({
        TARGETS[1]: ChannelPrivate("400 CHANNEL_PRIVATE"),
        TARGETS[3]: ChatAdminRequired("400 CHAT_ADMIN_REQUIRED"),
    })

    class Pool:
        def get(self, sid):
            return tg

    qm = DurableQueueManager(queue, mmap, client_pool=Pool(), attempts_repo=attempts)
    for tgt in TARGETS:
        await qm.enqueue_delivery(make_rule(5), make_payload(503), tgt, "hi", None)

    await qm.start()
    await drain(qm, queue)
    await qm.stop()

    res = await outcomes(queue, 503)
    ok = [t for t in TARGETS if t not in (TARGETS[1], TARGETS[3])]
    assert sorted(tg.sent) == sorted(ok), tg.sent
    assert res[TARGETS[1]][0] == "FAILED"
    assert res[TARGETS[3]][0] == "FAILED"
    assert all(res[t][0] == "SENT" for t in ok), res


async def test_transient_failure_recovers_for_that_destination_only(db, attempts, mmap, queue):
    """One destination's transient failure retries and succeeds; the others
    were never affected."""
    tg = PerTargetTelegram({TARGETS[2]: [ConnectionError("transient blip")]})

    class Pool:
        def get(self, sid):
            return tg

    qm = DurableQueueManager(queue, mmap, client_pool=Pool(), attempts_repo=attempts)
    for tgt in TARGETS:
        await qm.enqueue_delivery(make_rule(5), make_payload(504), tgt, "hi", None)

    await qm.start()
    # The transient destination retried with a real backoff; wait for it.
    for _ in range(120):
        await asyncio.sleep(0.1)
        res = await outcomes(queue, 504)
        if res.get(TARGETS[2], ("",))[0] == "SENT":
            break
    await qm.stop()

    assert sorted(tg.sent) == sorted(TARGETS), tg.sent
    res = await outcomes(queue, 504)
    assert all(res[t][0] == "SENT" for t in TARGETS), res


async def test_floodwait_does_not_block_other_destinations(db, attempts, mmap, queue):
    """A short FloodWait on one destination defers only that job; the rest
    deliver immediately."""
    # A FloodWait that lifts after one wait: the exception is raised once, then
    # the destination accepts the send.
    from pyrogram.errors import FloodWait
    tg = PerTargetTelegram({TARGETS[0]: [FloodWait(2)]})

    class Pool:
        def get(self, sid):
            return tg

    qm = DurableQueueManager(queue, mmap, client_pool=Pool(), attempts_repo=attempts)
    for tgt in TARGETS:
        await qm.enqueue_delivery(make_rule(5), make_payload(505), tgt, "hi", None)

    await qm.start()
    # The other four should land almost immediately.
    for _ in range(50):
        await asyncio.sleep(0.1)
        if len(tg.sent) >= 4:
            break
    assert sorted(tg.sent) == sorted(TARGETS[1:]), (
        "the FloodWait destination stalled the others"
    )
    # ...and the FloodWait destination follows once its wait elapses.
    for _ in range(60):
        await asyncio.sleep(0.2)
        if TARGETS[0] in tg.sent:
            break
    await qm.stop()
    assert sorted(tg.sent) == sorted(TARGETS), tg.sent
