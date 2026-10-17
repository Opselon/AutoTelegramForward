"""A burst of posts must reach a destination in the order it was posted.

Reported symptom: "if we send 50 messages at the same time they are not
forwarded to the target channel exactly in the order of the source channel".

Two defects produced it, and both are pinned here:

1. ``claim_batch`` ordered by ``created_at`` alone. A burst shares a one-second
   timestamp, so the order inside the burst was left to SQLite's row order.
2. Nothing stopped two of the three delivery workers from having a job in flight
   for the *same* destination at the same time, so even a correctly ordered
   claim set was delivered out of order by racing workers.

A third one survived that fix and is pinned here too: with ``created_at`` as the
*first* sort key, a burst that straddled a one-second boundary left in the order
the poller happened to read it, so a post with a higher message id overtook every
lower id (``test_a_burst_that_straddles_a_second_boundary_still_leaves_in_source_order``).

The fix gives every destination a lane: jobs are claimed in
``(source_chat_id, source_message_id, created_at, id)`` order — the source's own
order, with the ingest second only breaking ties inside one chat — and a job is
only claimable when no earlier unsent job exists for the same target. These tests
assert that property, not the SQL that implements it.
"""

from __future__ import annotations

import random
import asyncio
import time

import pytest

from core.domain.entities import DeliveryJob
from core.domain.value_objects import DeliveryStatus
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import SqliteDeliveryQueueRepository

TARGET = "-1003915313919"
OTHER_TARGET = "-1001854636317"


@pytest.fixture()
def repo(tmp_path):
    db = SqliteDatabase(str(tmp_path / "burst.db"))
    return db, SqliteDeliveryQueueRepository(db)


def _job(
    msg_id: int,
    *,
    target: str = TARGET,
    source: str = "-1004400615270",
    created_at: int | None = None,
) -> DeliveryJob:
    job = DeliveryJob(
        id=f"{source}:{msg_id}:{target}",
        rule_id="rule-burst",
        source_chat_id=source,
        source_message_id=msg_id,
        target_chat_id=target,
        payload_data={"text": f"post {msg_id}", "session_id": "s1"},
    )
    if created_at is not None:
        job.created_at = created_at
    return job


async def _drain_lane(repo, worker: str = "w1", batch: int = 5) -> list[int]:
    """Claim and confirm one job at a time; returns the message ids in sent order.

    This mirrors what a worker does: claim (bounded by the lane), send, mark sent.
    """
    sent: list[int] = []
    while True:
        claimed = await repo.claim_batch(worker_id=worker, batch_size=batch, lease_duration=30.0)
        if not claimed:
            break
        await repo.mark_sent_batch([(j.id, 1000 + j.source_message_id) for j in claimed])
        sent.extend(j.source_message_id for j in claimed)
    return sent


@pytest.mark.asyncio
async def test_a_burst_of_fifty_leaves_in_source_order(repo):
    """The reported case: 50 posts published together, one destination."""
    db, repo_ = repo
    ids = list(range(1, 51))
    shuffled = ids[:]
    random.Random(7).shuffle(shuffled)  # ingest order is not the source order
    for msg_id in shuffled:
        await repo_.enqueue(_job(msg_id))

    assert await _drain_lane(repo_) == ids


@pytest.mark.asyncio
async def test_a_burst_sharing_one_timestamp_is_ordered_by_message_id(repo):
    """created_at is second-resolution: a burst ties, the message id breaks it."""
    db, repo_ = repo
    stamp = int(time.time())
    for msg_id in (9, 3, 7, 1, 5):
        await repo_.enqueue(_job(msg_id, created_at=stamp))

    assert await _drain_lane(repo_) == [1, 3, 5, 7, 9]


@pytest.mark.asyncio
async def test_a_burst_that_straddles_a_second_boundary_still_leaves_in_source_order(repo):
    """The poller reading half the burst one second later must not reorder it.

    The regression this pins was found by a 50-post burst test that failed only
    on a slow runner: ``created_at`` led the sort key, so whatever the poller
    read first came out first. Here post 40 is read in the first second and the
    rest (ids 1..39) in the next, which is the exact shape of the failure — post
    40 overtook every lower id. The order a destination receives posts in is the
    order the *source* published them, not the order we happened to read them.
    """
    db, repo_ = repo
    stamp = int(time.time())
    await repo_.enqueue(_job(40, created_at=stamp))
    for msg_id in range(1, 40):
        await repo_.enqueue(_job(msg_id, created_at=stamp + 1))

    assert await _drain_lane(repo_) == list(range(1, 41))


@pytest.mark.asyncio
async def test_the_ingest_second_only_breaks_ties_within_one_source(repo):
    """Fan-in (A,B -> X): a later second on one source must not reorder the other.

    Both sources post 1,2,3; source B is read a second later. Each source keeps
    its own order, and the interleave stays stable (source_chat_id leads).
    """
    db, repo_ = repo
    stamp = int(time.time())
    for msg_id in (1, 2, 3):
        await repo_.enqueue(_job(msg_id, source="-1001111111", created_at=stamp))
        await repo_.enqueue(_job(msg_id, source="-1002222222", created_at=stamp + 1))

    assert await _drain_lane(repo_) == [1, 2, 3, 1, 2, 3]


@pytest.mark.asyncio
async def test_one_destination_has_at_most_one_job_in_flight(repo):
    """Two workers on one destination must not deliver concurrently."""
    db, repo_ = repo
    for msg_id in (1, 2, 3):
        await repo_.enqueue(_job(msg_id))

    first = await repo_.claim_batch(worker_id="w1", batch_size=5, lease_duration=30.0)
    assert [j.source_message_id for j in first] == [1]

    # A second worker, claiming *after* the first, must get nothing: the lane is
    # busy until job 1 is confirmed sent.
    assert await repo_.claim_batch(worker_id="w2", batch_size=5, lease_duration=30.0) == []

    await repo_.mark_sent_batch([(first[0].id, 1001)])
    second = await repo_.claim_batch(worker_id="w2", batch_size=5, lease_duration=30.0)
    assert [j.source_message_id for j in second] == [2]


@pytest.mark.asyncio
async def test_different_destinations_still_claim_in_parallel(repo):
    """Lanes are per destination: serialising one must not serialise the rest."""
    db, repo_ = repo
    for msg_id in (1, 2, 3):
        await repo_.enqueue(_job(msg_id, target=TARGET))
    for msg_id in (1, 2, 3):
        await repo_.enqueue(_job(msg_id, target=OTHER_TARGET))

    first = await repo_.claim_batch(worker_id="w1", batch_size=5, lease_duration=30.0)
    assert sorted((j.target_chat_id, j.source_message_id) for j in first) == [
        (OTHER_TARGET, 1), (TARGET, 1),
    ]


@pytest.mark.asyncio
async def test_a_retry_does_not_let_later_posts_overtake(repo):
    """Strict FIFO: while the lane head waits to be retried, the lane waits."""
    db, repo_ = repo
    for msg_id in (1, 2, 3):
        await repo_.enqueue(_job(msg_id))

    head = await repo_.claim_batch(worker_id="w1", batch_size=1, lease_duration=30.0)
    assert [j.source_message_id for j in head] == [1]
    await repo_.mark_retry(head[0].id, error="flood wait", backoff_seconds=30.0)

    # Post 2 is ready to send, but post 1 is still ahead of it in this lane:
    # delivering 2 now would put the burst out of order for good.
    assert await repo_.claim_batch(worker_id="w2", batch_size=5, lease_duration=30.0) == []

    # A permanent failure for the head releases the lane: nothing behind it is
    # skipped, and the posts that follow go out in order.
    await repo_.mark_failed(head[0].id, "permanent")
    stats = await repo_.get_stats()
    assert stats.get("RETRY_WAIT", 0) == 0, stats

    tail = await repo_.claim_batch(worker_id="w2", batch_size=5, lease_duration=30.0)
    assert [j.source_message_id for j in tail] == [2]


@pytest.mark.asyncio
async def test_two_sources_into_one_destination_stay_deterministic(repo):
    """Fan-in (A,B -> X) keeps each source's own order and a stable interleave."""
    db, repo_ = repo
    stamp = int(time.time())
    for msg_id in (1, 2, 3):
        await repo_.enqueue(_job(msg_id, source="-1001111111", created_at=stamp))
        await repo_.enqueue(_job(msg_id, source="-1002222222", created_at=stamp))

    sent = await _drain_lane(repo_)
    # Deterministic: the sort key leads with source_chat_id, so each source's
    # own order is preserved and the interleave of two sources is stable.
    assert sent == [1, 2, 3, 1, 2, 3]


@pytest.mark.asyncio
async def test_a_burst_fans_out_to_every_target_in_order(repo):
    """Fan-out (A -> X,Y) with a burst: each target gets it in source order.

    A message with several targets is enqueued once per target, so a burst
    becomes one lane per destination. The lanes are independent — neither target
    waits for the other — and inside a lane the order is still the source order,
    which is the property multi-target routing must not lose.
    """
    db, repo_ = repo
    ids = list(range(1, 21))
    shuffled = ids[:]
    random.Random(11).shuffle(shuffled)  # ingest order is not the source order
    for msg_id in shuffled:
        await repo_.enqueue(_job(msg_id, target=TARGET))
        await repo_.enqueue(_job(msg_id, target=OTHER_TARGET))

    first = await repo_.claim_batch(worker_id="w1", batch_size=5, lease_duration=30.0)
    # Both lanes are claimable in the same batch: one busy destination never
    # stalls the other.
    assert {j.target_chat_id for j in first} == {TARGET, OTHER_TARGET}
    assert {j.source_message_id for j in first} == {1}

    sent: dict[str, list[int]] = {TARGET: [], OTHER_TARGET: []}
    claimed = first
    while claimed:
        await repo_.mark_sent_batch([(j.id, 1000 + j.source_message_id) for j in claimed])
        for job in claimed:
            sent[job.target_chat_id].append(job.source_message_id)
        claimed = await repo_.claim_batch(worker_id="w1", batch_size=5, lease_duration=30.0)

    assert sent[TARGET] == ids
    assert sent[OTHER_TARGET] == ids


@pytest.mark.asyncio
async def test_an_expired_lease_on_the_lane_head_is_reclaimable(repo):
    """A crashed worker must not wedge its destination forever."""
    db, repo_ = repo
    for msg_id in (1, 2):
        await repo_.enqueue(_job(msg_id))

    head = await repo_.claim_batch(worker_id="dead", batch_size=1, lease_duration=0.05)
    assert [j.source_message_id for j in head] == [1]
    await asyncio.sleep(0.1)

    revived = await repo_.claim_batch(worker_id="alive", batch_size=5, lease_duration=30.0)
    assert [j.source_message_id for j in revived] == [1]
    assert revived[0].status == DeliveryStatus.CLAIMED
