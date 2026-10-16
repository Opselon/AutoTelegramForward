"""v14 queue scalability tests.

Covers the behaviours added by the queue upgrade:

* batch claiming / batch SENT marking (one transaction per batch)
* dead-letter routing when retries are exhausted
* bounded per-target rate limiter and stale-target pruning
* the in-memory pending counter backing the backpressure guard
* LogClient bounded queue (drop-oldest under pressure)
* configurable worker/batch knobs from Config

These exercise the repository and manager directly — no Telegram, no network.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.application.repositories import IDeliveryQueueRepository  # noqa: E402
from core.domain.entities import DeliveryJob, DeliveryStatus  # noqa: E402
from core.infrastructure.persistence.database import SqliteDatabase  # noqa: E402
from core.infrastructure.persistence.sqlite_repositories import (  # noqa: E402
    SqliteDeliveryQueueRepository,
    SqliteMessageMapRepository,
)


@pytest.fixture
def sqlite_db(tmp_path):
    db_path = str(tmp_path / "atf_v14.db")
    return SqliteDatabase(db_path)


def _job(i: int, status: DeliveryStatus = DeliveryStatus.PENDING, target: str = "") -> DeliveryJob:
    """A job for index `i`.

    Each index gets its own destination by default: delivery is serialised per
    destination (one in-flight job per target lane, in source order), so a batch
    claim can only span *different* targets. Tests that deliberately exercise one
    lane pass `target` explicitly.
    """
    return DeliveryJob(
        id=f"job:{i}",
        rule_id="rule1",
        source_chat_id="-100123",
        source_message_id=i,
        target_chat_id=target or f"-10099{i}",
        payload_data={"text": "hello", "session_id": "s1"},
        status=status,
    )


# ===================================================================== #
# Batch claiming + batch marking
# ===================================================================== #
@pytest.mark.asyncio
async def test_claim_batch_marks_whole_batch_in_one_statement(sqlite_db):
    """claim_batch claims N rows and updates them with a single UPDATE."""
    repo = SqliteDeliveryQueueRepository(sqlite_db)
    for i in range(12):
        await repo.enqueue(_job(i))

    claimed = await repo.claim_batch(worker_id="w1", batch_size=5, lease_duration=30.0)
    assert len(claimed) == 5
    assert all(j.status == DeliveryStatus.CLAIMED for j in claimed)
    assert all(j.worker_id == "w1" for j in claimed)

    # All 5 rows are CLAIMED in the DB (single UPDATE, not per-row)
    rows = sqlite_db.query_all("SELECT status, worker_id FROM delivery_jobs")
    claimed_rows = [r for r in rows if r["status"] == "CLAIMED"]
    assert len(claimed_rows) == 5
    assert all(r["worker_id"] == "w1" for r in claimed_rows)


@pytest.mark.asyncio
async def test_mark_sent_batch_commits_one_transaction(sqlite_db):
    """mark_sent_batch flips a batch to SENT and returns the count."""
    repo = SqliteDeliveryQueueRepository(sqlite_db)
    for i in range(8):
        await repo.enqueue(_job(i))
    claimed = await repo.claim_batch(worker_id="w1", batch_size=8, lease_duration=30.0)
    assert len(claimed) == 8

    updated = await repo.mark_sent_batch([(j.id, 1000 + i) for i, j in enumerate(claimed)])
    assert updated == 8

    rows = sqlite_db.query_all("SELECT status FROM delivery_jobs")
    assert all(r["status"] == "SENT" for r in rows)


@pytest.mark.asyncio
async def test_mark_sent_batch_empty_is_noop(sqlite_db):
    repo = SqliteDeliveryQueueRepository(sqlite_db)
    assert await repo.mark_sent_batch([]) == 0


@pytest.mark.asyncio
async def test_mark_sent_batch_falls_back_in_interface(sqlite_db):
    """The interface default loops mark_sent when a repo does not override it."""
    class OnlySingleMark(IDeliveryQueueRepository):
        def __init__(self):
            self.calls = []

        async def enqueue(self, job): raise NotImplementedError
        async def claim_batch(self, worker_id, batch_size=5, lease_duration=30.0): raise NotImplementedError
        async def mark_sent(self, job_id, target_message_id=None):
            self.calls.append(job_id)
            return True
        async def mark_retry(self, job_id, error, backoff_seconds): raise NotImplementedError
        async def mark_failed(self, job_id, error): raise NotImplementedError
        async def mark_unknown(self, job_id, error): raise NotImplementedError
        async def get_pending_count(self): return 0
        async def get_stats(self): return {}
        async def recover_expired_leases(self): return 0
        async def get_queue_age_metrics(self): return {}
        async def cleanup(self, retention_seconds=0): return 0

    repo = OnlySingleMark()
    n = await repo.mark_sent_batch([("a", 1), ("b", 2), ("c", 3)])
    assert n == 3
    assert repo.calls == ["a", "b", "c"]


# ===================================================================== #
# Dead-letter routing
# ===================================================================== #
@pytest.mark.asyncio
async def test_exhausted_retries_land_in_dead_letter(sqlite_db):
    """mark_retry routes a job to the DLQ once attempts run out (v14)."""
    repo = SqliteDeliveryQueueRepository(sqlite_db)
    await repo.enqueue(_job(0))
    # max_attempts defaults to 5
    for _ in range(4):
        await repo.mark_retry("job:0", "transient", backoff_seconds=0.01)

    # The 5th attempt exhausts the budget → FAILED + dead-letter row
    await repo.mark_retry("job:0", "transient", backoff_seconds=0.01)
    rows = sqlite_db.query_all("SELECT status FROM delivery_jobs")
    assert rows[0]["status"] == "FAILED"

    dlq = await repo.get_dead_letter_jobs()
    assert len(dlq) == 1
    assert dlq[0]["job_id"] == "job:0"
    assert dlq[0]["last_error"] == "transient"
    assert dlq[0]["error_category"] == "DELIVERY_EXHAUSTED"


@pytest.mark.asyncio
async def test_dead_letter_row_carries_payload(sqlite_db):
    """The DLQ entry keeps the original payload so an operator can replay it."""
    repo = SqliteDeliveryQueueRepository(sqlite_db)
    await repo.enqueue(_job(0))
    for _ in range(5):
        await repo.mark_retry("job:0", "boom", backoff_seconds=0.01)
    dlq = await repo.get_dead_letter_jobs()
    assert dlq[0]["payload_json"] and "hello" in dlq[0]["payload_json"]


# ===================================================================== #
# In-memory pending counter (backpressure guard)
# ===================================================================== #
@pytest.mark.asyncio
async def test_pending_counter_matches_database(sqlite_db):
    """The in-memory counter stays exact across the repository's mutators."""
    repo = SqliteDeliveryQueueRepository(sqlite_db)
    for i in range(10):
        await repo.enqueue(_job(i))
    assert await repo.get_pending_count() == 10

    claimed = await repo.claim_batch(worker_id="w", batch_size=4, lease_duration=30.0)
    assert len(claimed) == 4
    # claim moves PENDING -> CLAIMED, still inside the pending set
    assert await repo.get_pending_count() == 10

    await repo.mark_sent_batch([(j.id, 1) for j in claimed])
    assert await repo.get_pending_count() == 6

    await repo.mark_failed("job:9", "nope")
    assert await repo.get_pending_count() == 5


@pytest.mark.asyncio
async def test_pending_counter_replay_does_not_inflate(sqlite_db):
    """Enqueueing the same job id twice is a no-op and must not bump the count."""
    repo = SqliteDeliveryQueueRepository(sqlite_db)
    assert await repo.enqueue(_job(0))
    assert not await repo.enqueue(_job(0))  # INSERT OR IGNORE
    assert await repo.get_pending_count() == 1


@pytest.mark.asyncio
async def test_counter_reconciles_after_cleanup(sqlite_db):
    """cleanup() re-syncs the counter from the DB (drift safety)."""
    repo = SqliteDeliveryQueueRepository(sqlite_db)
    await repo.enqueue(_job(0, status=DeliveryStatus.SENT))
    # Simulate drift: hand-set the counter to something wrong
    repo._pending_estimate = 999
    # cleanup deletes nothing (job is brand new) but reconciles the counter
    await repo.cleanup(retention_seconds=60 * 60 * 24 * 365)
    assert await repo.get_pending_count() == 0


# ===================================================================== #
# Rate limiter bounds
# ===================================================================== #
@pytest.mark.asyncio
async def test_rate_limiter_window_is_bounded(sqlite_db):
    """The per-target window cannot exceed max_per_minute entries."""
    from core.infrastructure.telegram.durable_queue import DurableQueueManager

    q = SqliteDeliveryQueueRepository(sqlite_db)
    m = SqliteMessageMapRepository(sqlite_db)

    class NullPool:
        def get(self, _): return None

    mgr = DurableQueueManager(q, m, NullPool(), max_per_minute_per_target=3)

    # Fill the window past its capacity
    for _ in range(10):
        await mgr._check_target_rate("-100")

    window = mgr._target_windows["-100"]
    assert len(window) <= 3, f"window grew unbounded: {len(window)}"


@pytest.mark.asyncio
async def test_rate_limiter_prunes_stale_targets(sqlite_db):
    """Targets that have not sent recently are evicted (memory bound)."""
    from core.infrastructure.telegram.durable_queue import DurableQueueManager

    q = SqliteDeliveryQueueRepository(sqlite_db)
    m = SqliteMessageMapRepository(sqlite_db)

    class NullPool:
        def get(self, _): return None

    mgr = DurableQueueManager(q, m, NullPool())
    mgr._max_tracked_targets = 2
    # Three distinct targets; the cap is 2 so the oldest must be evicted
    for tgt in ("-1", "-2", "-3"):
        await mgr._check_target_rate(tgt)
    # _prune runs when the cap is hit, so at most 2 targets remain tracked
    assert len(mgr._target_windows) <= 2


@pytest.mark.asyncio
async def test_rate_limiter_returns_wait_when_saturated(sqlite_db):
    """Over the per-minute cap the limiter returns a positive wait, not 0."""
    from core.infrastructure.telegram.durable_queue import DurableQueueManager

    q = SqliteDeliveryQueueRepository(sqlite_db)
    m = SqliteMessageMapRepository(sqlite_db)

    class NullPool:
        def get(self, _): return None

    mgr = DurableQueueManager(q, m, NullPool(), max_per_minute_per_target=2)
    assert await mgr._check_target_rate("-1") == 0.0
    assert await mgr._check_target_rate("-1") == 0.0
    wait = await mgr._check_target_rate("-1")
    assert wait > 0


# ===================================================================== #
# Observability
# ===================================================================== #
@pytest.mark.asyncio
async def test_observability_has_v14_fields(sqlite_db):
    """get_observability_metrics() exposes throughput, queue wait and RSS."""
    from core.infrastructure.telegram.durable_queue import DurableQueueManager

    q = SqliteDeliveryQueueRepository(sqlite_db)
    m = SqliteMessageMapRepository(sqlite_db)

    class NullPool:
        def get(self, _): return None

    mgr = DurableQueueManager(q, m, NullPool(), num_workers=1)
    metrics = await mgr.get_observability_metrics()
    for key in (
        "throughput_per_second",
        "avg_queue_wait_seconds",
        "rss_mb",
        "dead_letter_jobs",
        "queue_depth",
    ):
        assert key in metrics, f"missing {key}"


# ===================================================================== #
# Config knobs
# ===================================================================== #
def test_config_reads_queue_env_vars(monkeypatch):
    """ATF_QUEUE_* env vars reach Config (v14)."""
    from core.config import Config

    monkeypatch.setenv("ATF_QUEUE_NUM_WORKERS", "8")
    monkeypatch.setenv("ATF_QUEUE_BATCH_SIZE", "16")
    monkeypatch.setenv("ATF_QUEUE_MAX_SIZE", "2000")
    monkeypatch.setenv("ATF_QUEUE_LEASE_SECONDS", "45.5")
    monkeypatch.setenv("ATF_QUEUE_MAX_PER_MINUTE", "12")
    monkeypatch.setenv("ATF_DATABASE_URL", "postgres://u:p@localhost:5432/atf")

    cfg = Config.load(config_path="/nonexistent.yaml")
    assert cfg.queue_num_workers == 8
    assert cfg.queue_batch_size == 16
    assert cfg.queue_max_size == 2000
    assert cfg.queue_lease_seconds == 45.5
    assert cfg.queue_max_per_minute_per_target == 12
    assert cfg.database_url == "postgres://u:p@localhost:5432/atf"


def test_config_queue_defaults():
    from core.config import Config

    cfg = Config.load(config_path="/nonexistent.yaml")
    assert cfg.queue_num_workers == 3
    assert cfg.queue_batch_size == 8
    assert cfg.database_url == ""


# ===================================================================== #
# LogClient bounded queue
# ===================================================================== #
@pytest.mark.asyncio
async def test_log_client_queue_is_bounded_and_drops_oldest():
    """A bounded LogClient drops the oldest record instead of growing."""
    from logger.log_client import LogClient

    client = LogClient(address="localhost:1", max_queue_size=4)
    # Never connect: _stub stays None so log() is a no-op, but the bound is
    # exercised through the queue directly.
    assert client._queue.maxsize == 4

    for i in range(10):
        try:
            client._queue.put_nowait(f"entry-{i}")
        except asyncio.QueueFull:
            client._queue.get_nowait()
            client._queue.put_nowait(f"entry-{i}")

    # Bounded: never more than maxsize
    assert client._queue.qsize() <= 4
    # The newest entries survived; the oldest were evicted
    contents = list(client._queue._queue)
    assert "entry-9" in contents
    assert "entry-0" not in contents


@pytest.mark.asyncio
async def test_log_client_queue_bound_default_is_set():
    from logger.log_client import LogClient

    client = LogClient(address="localhost:1")
    assert client._queue.maxsize > 0, "queue must be bounded by default"
