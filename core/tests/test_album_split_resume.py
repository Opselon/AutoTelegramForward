"""E2E-5: a split-album retry must not re-send items Telegram already accepted.

A 3-item album in ``split`` mode. Items 50 and 51 are accepted by Telegram;
item 52 fails once with a 500. The job goes to RETRY_WAIT with two items
physically in the target.

**Before the fix** the retry re-sent items 50 and 51 — the items Telegram had
already accepted — producing duplicates in the destination channel.
**After the fix** the job payload records the confirmed per-item ids and the
retry resumes from the first *unconfirmed* item.

Deterministic and in-process: the queue, the repositories, the worker and the
lease are real; only Telegram is faked, with controlled failure injection on
one item.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

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
TARGET = "-100T"


class ItemTelegram:
    """Deterministic copy target whose failures are configured per item id."""

    def __init__(self, fail_items: dict = None):
        # source message id -> exception instance or a list of them (per attempt)
        self.fail_items = fail_items or {}
        self.copies: list[int] = []
        self.calls = 0

    async def copy_message(self, chat_id, from_chat_id, message_id, caption=None, **kw):
        self.calls += 1
        failure = self.fail_items.get(int(message_id))
        if failure is not None:
            if isinstance(failure, list):
                if failure:
                    raise failure.pop(0)
            else:
                raise failure
        self.copies.append(int(message_id))
        self.calls += 1
        class _R:
            id = 1000 + self.calls
        return _R()



@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "album.db"))


@pytest.fixture
def attempts(db):
    return SqliteDeliveryAttemptRepository(db)


@pytest.fixture
def mmap(db):
    return SqliteMessageMapRepository(db)


@pytest.fixture
def queue(db):
    return SqliteDeliveryQueueRepository(db)


def make_rule():
    # The queue reads the album mode from the rule (``album_mode`` attribute),
    # defaulting to "album" (one grouped post) when unset.
    rule = ForwardRule(
        id="rule-1", session_id="sess", source_chat_id=SOURCE,
        target_chat_id=TARGET, target_chat_ids=[TARGET], is_active=True,
    )
    rule.album_mode = "split"
    return rule


def make_album_payload(album_ids):
    # The worker reads album membership from ``album_messages`` and falls back
    # to the single message id when it is empty.
    items = [SimpleNamespace(message_id=i) for i in album_ids]
    return MessagePayload(
        message_id=album_ids[0], chat_id=SOURCE, text="", caption="",
        has_media=True, media_type="photo", media_group_id="album-1",
        album_messages=items,
    )


class Pool:
    def __init__(self, tg):
        self._tg = tg

    def get(self, sid):
        return self._tg


async def drain_until_done(queue, job_id: str, timeout: float = 20.0) -> str:
    """Wait until the job reaches a terminal state, or fail the test.

    A job in RETRY_WAIT is made eligible again immediately: the real watchdog
    honours the exponential backoff, but the *outcome* under test here is
    which items get re-sent, not when — sleeping would only slow the suite.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = queue._db.query_one(
            "SELECT status, next_retry_at FROM delivery_jobs WHERE id = ?", (job_id,)
        )
        if row is None:
            raise AssertionError(f"job {job_id} disappeared")
        if row["status"] in ("SENT", "FAILED", "DEAD"):
            return row["status"]
        if row["status"] == "RETRY_WAIT":
            queue._db.execute(
                "UPDATE delivery_jobs SET status='PENDING', lease_until=0, "
                "next_retry_at=0 WHERE id = ?",
                (job_id,),
            )
        await asyncio.sleep(0.05)
    row = queue._db.query_one("SELECT status FROM delivery_jobs WHERE id = ?", (job_id,))
    raise AssertionError(f"job did not finish: {row['status']}")


def job_payload(queue, job_id: str) -> dict:
    row = queue._db.query_one(
        "SELECT payload_json FROM delivery_jobs WHERE id = ?", (job_id,)
    )
    raw = row["payload_json"]
    return json.loads(raw) if isinstance(raw, str) else raw


def enqueued_job_id(queue, message_id: int) -> str:
    row = queue._db.query_one(
        "SELECT id FROM delivery_jobs WHERE source_message_id = ?", (message_id,)
    )
    return row["id"]


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #

async def test_a_split_album_retry_resumes_from_the_first_unconfirmed_item(
    db, attempts, mmap, queue
):
    """The E2E-5 contract: the retry sends only the items not yet confirmed."""
    tg = ItemTelegram(fail_items={52: [RuntimeError("500 INTERNAL")]})
    qm = DurableQueueManager(queue, mmap, client_pool=Pool(tg), attempts_repo=attempts)

    await qm.enqueue_delivery(
        make_rule(), make_album_payload([50, 51, 52]), TARGET, "", None
    )
    job_id = enqueued_job_id(queue, 50)
    await qm.start()
    try:
        status = await drain_until_done(queue, job_id)
    finally:
        await qm.stop()

    copies = sorted(tg.copies)
    duplicates = sorted({i for i in tg.copies if tg.copies.count(i) > 1})

    assert status == "SENT", f"job should complete, got {status}"
    assert copies == [50, 51, 52], f"expected each item once, got {copies}"
    assert not duplicates, f"items re-sent by the retry: {duplicates}"
    # The ids the fake assigned, in the order the items were confirmed. What
    # matters is the *set* of keys (every item, confirmed exactly once) and
    # that no item was re-sent — pinned by copies and duplicates above.
    sent = job_payload(queue, job_id).get("album_sent") or {}
    assert sorted(sent.keys()) == ["50", "51", "52"], sent
    assert sorted(sent.values()) == sorted(set(sent.values())), sent


async def test_progress_is_recorded_before_the_job_raises(db, attempts, mmap, queue):
    """Items accepted before the failure are recorded even while the job fails."""
    tg = ItemTelegram(fail_items={51: RuntimeError("500 INTERNAL")})
    qm = DurableQueueManager(queue, mmap, client_pool=Pool(tg), attempts_repo=attempts)

    await qm.enqueue_delivery(
        make_rule(), make_album_payload([50, 51, 52]), TARGET, "", None
    )
    job_id = enqueued_job_id(queue, 50)
    await qm.start()
    try:
        # The job will not complete: item 51 fails permanently. Give the worker
        # one full attempt, then stop it and inspect the payload.
        await asyncio.sleep(0.6)
    finally:
        await qm.stop()

    # The split loop is ordered: it stops at the failing item, so item 51 has
    # not been sent on this attempt (and neither has 52, which comes after it).
    # The retry later picks up from 51 — that is the resume contract.
    assert 50 in tg.copies, f"item 50 was sent before the failure, got {tg.copies}"
    # Item 50 was accepted before the failure and must be recorded as confirmed,
    # otherwise the retry would duplicate it.
    assert job_payload(queue, job_id).get("album_sent", {}).get("50") is not None, (
        "an item accepted before the failure was not recorded"
    )


async def test_a_permanently_failing_item_does_not_duplicate_the_rest(
    db, attempts, mmap, queue
):
    """Exhausting retries on one item must not duplicate its siblings."""
    tg = ItemTelegram(fail_items={51: RuntimeError("400 CHAT_ADMIN_REQUIRED")})
    qm = DurableQueueManager(queue, mmap, client_pool=Pool(tg), attempts_repo=attempts)

    await qm.enqueue_delivery(
        make_rule(), make_album_payload([50, 51, 52]), TARGET, "", None
    )
    job_id = enqueued_job_id(queue, 50)
    await qm.start()
    try:
        status = await drain_until_done(queue, job_id)
    finally:
        await qm.stop()

    copies = sorted(tg.copies)
    duplicates = sorted({i for i in tg.copies if tg.copies.count(i) > 1})
    # The item that succeeded (50) is never re-sent, no matter how many times
    # its sibling fails — the duplicate guarantee this test exists for.
    assert not duplicates, f"exhausted retries duplicated items: {duplicates}"
    assert tg.copies.count(50) == 1, f"item 50 re-sent: {tg.copies}"
