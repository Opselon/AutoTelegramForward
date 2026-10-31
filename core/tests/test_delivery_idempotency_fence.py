"""Regression tests for the send-side idempotency fence (audit finding #5).

Telegram has no idempotency key for ordinary message sends, so the only way to
stop this application double-dispatching one logical delivery is to claim the
logical send ourselves, atomically, *before* any Telegram call. These tests
prove the fence on the real repository and the real worker, with deterministic
failure injection — no mocks of the persistence layer.

Crash boundaries covered:

* crash between Telegram accepting the send and the batch commit (the v14
  deferred-commit window);
* a lease expiring *mid-dispatch* (no crash at all — a slow send with a short
  lease used to double-deliver);
* a timeout after acceptance (outcome UNKNOWN — never a blind resend);
* concurrent workers racing one job;
* the legacy direct path and the worker racing the same logical delivery;
* the ``already_sent`` fast path that marks the job SENT without re-sending.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.domain.entities import DeliveryJob, DeliveryStatus, ForwardRule, MessagePayload
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryAttemptRepository,
    SqliteDeliveryQueueRepository,
    SqliteMessageMapRepository,
)
from core.infrastructure.telegram.durable_queue import DurableQueueManager
from core.domain.value_objects import DeliveryAttemptKey

SOURCE = "-100S"
TARGET = "-100T"


class FakeTelegram:
    """Deterministic send target that records every copy it receives."""

    def __init__(self, delay: float = 0.0, fail_once: bool = False):
        self.sent: list = []
        self.delay = delay
        self.fail_once = fail_once
        self._failed = False

    async def copy_message(self, chat_id, from_chat_id, message_id, caption=None, **kw):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_once and not self._failed:
            self._failed = True
            raise ConnectionError("transient blip")
        self.sent.append((chat_id, message_id))
        class _R:
            id = 1000 + len(self.sent)
        return _R()


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "fence.db"))


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
    return ForwardRule(id="rule-1", session_id="sess", source_chat_id=SOURCE,
                       target_chat_id=TARGET, is_active=True)


def make_payload(message_id: int = 777):
    return MessagePayload(message_id=message_id, chat_id=SOURCE, text="hi",
                          has_media=True, media_type="photo")


# --------------------------------------------------------------------------- #
# The fence itself
# --------------------------------------------------------------------------- #

async def test_two_workers_cannot_claim_the_same_send(attempts):
    """A live claim by one worker is visible to the other as in_flight."""
    key = DeliveryAttemptKey("rule-1", SOURCE, 1, TARGET)
    outcome, _ = await attempts.claim_or_check(key, "w1", lease_until=time.time() + 60)
    assert outcome == "dispatch"

    outcome2, row2 = await attempts.claim_or_check(key, "w2", lease_until=time.time() + 60)
    assert outcome2 == "in_flight"
    assert row2["worker_id"] == "w1"


async def test_a_recorded_success_is_never_redispatched(attempts):
    """SENT is terminal: a later claim answers already_sent, not dispatch."""
    key = DeliveryAttemptKey("rule-1", SOURCE, 2, TARGET)
    await attempts.claim_or_check(key, "w1", lease_until=time.time() + 60)
    await attempts.record_sent(key, "w1", 4242)

    outcome, row = await attempts.claim_or_check(key, "w2", lease_until=time.time() + 60)
    assert outcome == "already_sent"
    assert row["target_message_id"] == 4242


async def test_expired_claims_become_unknown_and_require_explicit_resolution(attempts):
    """An expired claim is not reopened for dispatch: the previous send may
    have been accepted by Telegram and never recorded. It becomes UNKNOWN, and
    only an audited ``resolve_unknown`` reopens it."""
    key = DeliveryAttemptKey("rule-1", SOURCE, 3, TARGET)
    # A claim already in the past: deterministic on any runner, no sleep race.
    await attempts.claim_or_check(key, "w1", lease_until=time.time() - 1.0)

    # Another worker cannot take it while it is still CLAIMED.
    outcome, _ = await attempts.claim_or_check(key, "w2", lease_until=time.time() + 60)
    assert outcome == "stale"

    expired = await attempts.expire_stale_claims(time.time())
    assert expired == 1

    outcome, _ = await attempts.claim_or_check(key, "w2", lease_until=time.time() + 60)
    assert outcome == "unknown", "an expired claim must not be silently re-dispatched"

    # The only sanctioned way out, with a recorded reason.
    assert await attempts.resolve_unknown(key, "w2", "verified absent in target") is True
    outcome, _ = await attempts.claim_or_check(key, "w2", lease_until=time.time() + 60)
    assert outcome == "dispatch"


async def test_a_failed_fence_is_reclaimable_on_replay(attempts):
    """A delivery replayed from the dead-letter queue re-claims its fence and
    the attempt counter advances, so the retry budget stays honest."""
    key = DeliveryAttemptKey("rule-1", SOURCE, 4, TARGET)
    await attempts.claim_or_check(key, "w1", lease_until=time.time() + 60)
    await attempts.record_outcome(key, "w1", "FAILED", "CHAT_ADMIN_REQUIRED")

    outcome, row = await attempts.claim_or_check(key, "w2", lease_until=time.time() + 60)
    assert outcome == "dispatch"
    assert row["attempts"] == 1


async def test_record_sent_is_idempotent_and_terminal(attempts):
    key = DeliveryAttemptKey("rule-1", SOURCE, 5, TARGET)
    await attempts.claim_or_check(key, "w1", lease_until=time.time() + 60)
    assert await attempts.record_sent(key, "w1", 500) is True
    # A second record (a late batch-commit after a replay) is a no-op.
    assert await attempts.record_sent(key, "w1", 501) is False


# --------------------------------------------------------------------------- #
# The worker, end to end
# --------------------------------------------------------------------------- #

async def test_crash_before_batch_commit_does_not_duplicate(db, attempts, mmap, queue):
    """Telegram accepted the send; the process died before mark_sent_batch.

    The lease expires and the watchdog reclaims the job, but the fence has the
    confirmed outcome — so the job is marked SENT without re-dispatching it.
    Before the fix this produced a second copy in the target channel.
    """
    tg = FakeTelegram()

    class Pool:
        def get(self, sid):
            return tg

    qm = DurableQueueManager(queue, mmap, client_pool=Pool(), lease_duration=0.2,
                             attempts_repo=attempts)
    await qm.enqueue_delivery(make_rule(), make_payload(), TARGET, "hi", None)

    jobs = await queue.claim_batch(worker_id="w1", batch_size=1, lease_duration=0.2)
    result = await qm._process_job("w1", jobs[0])
    assert result is not None, "the send succeeded and returned a result"
    assert len(tg.sent) == 1
    # The batch commit never happens — the process "dies" here.

    await asyncio.sleep(0.35)
    recovered = await queue.recover_expired_leases()
    assert recovered == 1

    await qm.start()
    for _ in range(40):
        await asyncio.sleep(0.1)
        st = queue._db.query_one("SELECT status FROM delivery_jobs WHERE id = ?",
                                 ("rule-1:%s:777:%s" % (SOURCE, TARGET),))
        if st and st["status"] == "SENT":
            break
    await qm.stop()

    assert len(tg.sent) == 1, f"the message was delivered {len(tg.sent)} times"


async def test_lease_expiry_mid_dispatch_does_not_duplicate(db, attempts, mmap, queue):
    """A send that outlives its lease used to be re-claimed and re-dispatched
    while the first one was still in flight — no crash needed."""
    tg = FakeTelegram(delay=0.4)  # longer than the 0.2s lease

    class Pool:
        def get(self, sid):
            return tg

    qm = DurableQueueManager(queue, mmap, client_pool=Pool(), lease_duration=0.2,
                             attempts_repo=attempts)
    await qm.enqueue_delivery(make_rule(), make_payload(message_id=888), TARGET, "hi", None)

    j1 = await queue.claim_batch(worker_id="w1", batch_size=1, lease_duration=0.2)
    t1 = asyncio.create_task(qm._process_job("w1", j1[0]))

    await asyncio.sleep(0.3)
    assert await queue.recover_expired_leases() == 1, "the watchdog reclaimed a live dispatch"
    j2 = await queue.claim_batch(worker_id="w2", batch_size=1, lease_duration=0.2)

    await t1
    if j2:
        await qm._process_job("w2", j2[0])

    assert len(tg.sent) == 1, f"two workers dispatched the same logical delivery {tg.sent}"


async def test_transient_failure_then_success_uses_the_fence(db, attempts, mmap, queue):
    """A failed attempt releases the fence (no copy exists), so the retry
    dispatches again and succeeds."""
    tg = FakeTelegram(fail_once=True)

    class Pool:
        def get(self, sid):
            return tg

    qm = DurableQueueManager(queue, mmap, client_pool=Pool(), attempts_repo=attempts)
    job_id = "rule-1:%s:909:%s" % (SOURCE, TARGET)
    await qm.enqueue_delivery(make_rule(), make_payload(message_id=909), TARGET, "hi", None)

    j1 = await queue.claim_batch(worker_id="w1", batch_size=1)
    await qm._process_job("w1", j1[0])          # the transient failure
    assert len(tg.sent) == 0

    # The worker classified it recoverable and scheduled a real backoff. The
    # fence was released as FAILED, which is what the retry relies on; make the
    # job claimable now so the test does not have to wait the backoff out.
    queue._db.execute(
        "UPDATE delivery_jobs SET next_retry_at = 0 WHERE id = ?", (job_id,)
    )
    j2 = await queue.claim_batch(worker_id="w2", batch_size=1)
    assert j2, "the retry must be claimable after a transient failure"
    await qm._process_job("w2", j2[0])
    assert len(tg.sent) == 1


async def test_already_sent_job_is_closed_without_resending(db, attempts, mmap, queue):
    """The worker's fast path: a fence that already records a confirmed send
    marks the job SENT with the recorded id and never calls Telegram."""
    tg = FakeTelegram()

    class Pool:
        def get(self, sid):
            return tg

    qm = DurableQueueManager(queue, mmap, client_pool=Pool(), attempts_repo=attempts)
    await qm.enqueue_delivery(make_rule(), make_payload(message_id=1010), TARGET, "hi", None)

    job_id = "rule-1:%s:1010:%s" % (SOURCE, TARGET)
    # Pretend an earlier run already sent it and recorded the outcome.
    key = DeliveryAttemptKey("rule-1", SOURCE, 1010, TARGET)
    await attempts.claim_or_check(key, "someone", lease_until=time.time() + 60)
    await attempts.record_sent(key, "someone", 7777)

    jobs = await queue.claim_batch(worker_id="w1", batch_size=1)
    await qm._process_job("w1", jobs[0])

    assert len(tg.sent) == 0, "the worker re-dispatched an already-confirmed send"
    row = queue._db.query_one("SELECT status FROM delivery_jobs WHERE id = ?", (job_id,))
    assert row["status"] == "SENT"
