"""Part 2 STEER 06 — queue, concurrency and idempotency, proved on the real store.

The durable queue is the boundary where "forwarded once" can quietly become
"forwarded twice" or "never". These tests drive the real
``DurableQueueManager`` and the real ``SqliteDeliveryQueueRepository`` (claim
gate, lease, retry, dead-letter) rather than a stub, and they check the two
properties an operator actually relies on:

* one job, one delivery — a second worker cannot claim a job whose lease is
  live, and a claimed job is never duplicated by a replay;
* nothing is lost — a lease that expires is reclaimed, a retry waits its
  backoff, an exhausted job lands in the dead-letter queue **with its payload**
  and stays visible to its own tenant only.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from core.domain.entities import DeliveryJob, ForwardRule, MessagePayload, TelegramSession
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryQueueRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.durable_queue import DurableQueueManager

OWNER_A = 5094837833
OWNER_B = 5989354154
SOURCE = "-1004400615270"
TARGET_A = "-1003915313919"
TARGET_B = "-1003915313920"


def make_rule(rule_id: str, owner: int = OWNER_A, target: str = TARGET_A,
              session: str = "sess-a", **kwargs) -> ForwardRule:
    return ForwardRule(id=rule_id, session_id=session, source_chat_id=SOURCE,
                       target_chat_id=target, is_active=True, owner_user_id=owner, **kwargs)


def payload(message_id: int = 1, text: str = "hello") -> MessagePayload:
    return MessagePayload(message_id=message_id, chat_id=SOURCE, text=text)


async def seed(db, *rules) -> None:
    sessions = SqliteSessionRepository(db, CryptoService("test-key"))
    for rule in rules:
        if await sessions.get_by_id(rule.session_id) is None:
            await sessions.add(TelegramSession(
                id=rule.session_id, phone_number="+100****0000", is_active=True,
                is_authorized=True, owner_user_id=rule.owner_user_id,
            ))
    repo = SqliteForwardRuleRepository(db)
    for rule in rules:
        await repo.add(rule)


def make_queue(db, workers: int = 2) -> DurableQueueManager:
    return DurableQueueManager(
        queue_repo=SqliteDeliveryQueueRepository(db),
        message_map_repo=SqliteMessageMapRepository(db),
        client_pool=MagicMock(),
        rule_repo=SqliteForwardRuleRepository(db),
        num_workers=workers,
    )


async def enqueue(db, rule_id: str, message_id: int, text: str = "hello",
                  target: str = "", job_kind: str = "forward") -> bool:
    rule = await SqliteForwardRuleRepository(db).get_by_id(rule_id)
    destination = target or str(getattr(rule, "target_chat_id", "") or TARGET_A)
    return await make_queue(db).enqueue_delivery(
        rule, payload(message_id, text), destination, text, job_kind=job_kind,
    )


def jobs_rows(db, where: str = "1=1") -> list:
    return [dict(r) for r in db.query_all(f"SELECT * FROM delivery_jobs WHERE {where}")]


@pytest.fixture
def db(tmp_path):
    database = SqliteDatabase(str(tmp_path / "queue.db"))
    try:
        yield database
    finally:
        database.close()


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_enqueueing_the_same_event_twice_produces_one_job(db):
    async def run():
        await seed(db, make_rule("r-1"))
        assert await enqueue(db, "r-1", 5, "only once") is True
        assert await enqueue(db, "r-1", 5, "only once") is False, "the replay was accepted"
        assert len(jobs_rows(db)) == 1, jobs_rows(db)

    asyncio.run(run())


def test_a_keyword_reply_is_not_deduplicated_away_by_the_forward(db):
    """A distinct deliverable needs a distinct id, or it is silently dropped."""
    async def run():
        await seed(db, make_rule("r-2"))
        assert await enqueue(db, "r-2", 6, "body") is True
        assert await enqueue(db, "r-2", 6, "the reply", job_kind="keyword_reply") is True
        kinds = {str(r["id"]).split(":")[-1] for r in jobs_rows(db)}
        assert len(jobs_rows(db)) == 2, jobs_rows(db)
        assert "keyword_reply" in kinds, kinds

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Concurrency: one job, one delivery
# --------------------------------------------------------------------------- #
def test_a_live_lease_is_not_reclaimable_by_a_second_worker(db):
    async def run():
        await seed(db, make_rule("r-3"))
        await enqueue(db, "r-3", 7)
        repo = SqliteDeliveryQueueRepository(db)
        first = await repo.claim_batch(worker_id="worker-1", batch_size=5, lease_duration=60)
        assert [j.id for j in first], first
        second = await repo.claim_batch(worker_id="worker-2", batch_size=5, lease_duration=60)
        assert second == [], "two workers claimed the same job"
        rows = jobs_rows(db, "status='CLAIMED'")
        assert {r["worker_id"] for r in rows} == {"worker-1"}, rows

    asyncio.run(run())


def test_an_expired_lease_is_reclaimed_as_the_same_job(db):
    """At-least-once, and never a second row: the crash case must not fork."""
    async def run():
        await seed(db, make_rule("r-4"))
        await enqueue(db, "r-4", 8)
        repo = SqliteDeliveryQueueRepository(db)
        first = await repo.claim_batch(worker_id="worker-1", batch_size=5, lease_duration=30)
        job_id = first[0].id
        db.connection.execute("UPDATE delivery_jobs SET lease_until = 0")
        db.connection.commit()
        again = await repo.claim_batch(worker_id="worker-2", batch_size=5, lease_duration=30)
        assert [j.id for j in again] == [job_id], again
        assert len(jobs_rows(db)) == 1, jobs_rows(db)

    asyncio.run(run())


def test_a_burst_leaves_in_source_order_for_one_target(db):
    """A destination is a single lane: drain it one job at a time, in order."""
    async def run():
        await seed(db, make_rule("r-5"))
        for message_id in (30, 10, 20):
            await enqueue(db, "r-5", message_id)
        repo = SqliteDeliveryQueueRepository(db)
        seen = []
        for _ in range(3):
            claimed = await repo.claim_batch(worker_id="w", batch_size=10, lease_duration=30)
            assert len(claimed) == 1, [j.id for j in claimed]
            seen.append(claimed[0].source_message_id)
            await repo.mark_sent(claimed[0].id, 1)
        assert seen == [10, 20, 30], seen

    asyncio.run(run())


def test_a_destination_is_a_single_lane_across_tasks(db):
    """Two jobs to one target never run concurrently: the second waits."""
    async def run():
        await seed(db, make_rule("r-6a"), make_rule("r-6b"))
        await enqueue(db, "r-6a", 11, target=TARGET_A)
        await enqueue(db, "r-6b", 12, target=TARGET_A)
        claimed = await SqliteDeliveryQueueRepository(db).claim_batch(
            worker_id="w", batch_size=10, lease_duration=30)
        assert len(claimed) == 1, [j.id for j in claimed]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Retry, backoff and the dead-letter queue
# --------------------------------------------------------------------------- #
def test_a_retry_waits_its_backoff_then_comes_back(db):
    async def run():
        await seed(db, make_rule("r-7"))
        await enqueue(db, "r-7", 13)
        repo = SqliteDeliveryQueueRepository(db)
        job = (await repo.claim_batch(worker_id="w", batch_size=5, lease_duration=30))[0]
        assert await repo.mark_retry(job.id, "FloodWait", 120.0) is True
        row = jobs_rows(db)[0]
        assert row["status"] == "RETRY_WAIT", row
        assert int(row["attempts"]) == 1, row
        assert row["next_retry_at"] > time.time() + 60, row
        assert await repo.claim_batch(worker_id="w2", batch_size=5, lease_duration=30) == [], "retried too early"
        db.connection.execute("UPDATE delivery_jobs SET next_retry_at = 0")
        db.connection.commit()
        back = await repo.claim_batch(worker_id="w2", batch_size=5, lease_duration=30)
        assert [j.id for j in back] == [job.id], back

    asyncio.run(run())


def test_an_exhausted_job_lands_in_the_dead_letter_queue_with_its_payload(db):
    async def run():
        await seed(db, make_rule("r-8"))
        await enqueue(db, "r-8", 14, text="keep me for replay")
        repo = SqliteDeliveryQueueRepository(db)
        job = (await repo.claim_batch(worker_id="w", batch_size=5, lease_duration=30))[0]
        db.connection.execute("UPDATE delivery_jobs SET max_attempts = 1 WHERE id = ?", (job.id,))
        db.connection.commit()
        claimed = (await repo.claim_batch(worker_id="w", batch_size=5, lease_duration=30))
        assert [j.id for j in claimed] in ([], [job.id]), claimed

        assert await repo.mark_retry(job.id, "CHAT_WRITE_FORBIDDEN", 1.0) is True
        row = jobs_rows(db)[0]
        assert row["status"] == "FAILED", row
        letters = await repo.list_dead_letters(rule_ids=["r-8"])
        assert len(letters) == 1, letters
        assert "keep me for replay" in str(letters[0].get("payload_json", "")), letters[0]
        assert await repo.claim_batch(worker_id="w3", batch_size=5, lease_duration=30) == [], "an exhausted job came back"

    asyncio.run(run())


def test_the_dead_letter_queue_is_scoped_to_the_callers_rules(db):
    """Tenant B's failed delivery is invisible to tenant A's console."""
    async def run():
        await seed(db, make_rule("r-9a", owner=OWNER_A),
                   make_rule("r-9b", owner=OWNER_B, target=TARGET_B, session="sess-b"))
        for rule_id in ("r-9a", "r-9b"):
            await enqueue(db, rule_id, 15)
        repo = SqliteDeliveryQueueRepository(db)
        claimed = await repo.claim_batch(worker_id="w", batch_size=10, lease_duration=30)
        assert len(claimed) == 2, [j.id for j in claimed]
        for job in claimed:
            db.connection.execute("UPDATE delivery_jobs SET max_attempts = 1 WHERE id = ?", (job.id,))
        db.connection.commit()
        for job in claimed:
            assert await repo.mark_retry(job.id, "CHAT_WRITE_FORBIDDEN", 1.0) is True

        assert await repo.count_dead_letters(rule_ids=["r-9a"]) == 1
        assert await repo.count_dead_letters(rule_ids=["r-9b"]) == 1
        a_letters = await repo.list_dead_letters(rule_ids=["r-9a"])
        assert {str(e.get("rule_id")) for e in a_letters} == {"r-9a"}, a_letters
        assert await repo.list_dead_letters(rule_ids=[]) == [], "an empty scope must match nothing"
        assert await repo.claim_batch(worker_id="w3", batch_size=5, lease_duration=30) == [], "an exhausted job came back"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Durability
# --------------------------------------------------------------------------- #
def test_a_pending_job_survives_a_reopen_with_its_text(db, tmp_path):
    async def first():
        await seed(db, make_rule("r-10"))
        await enqueue(db, "r-10", 16, text="survive the restart")

    asyncio.run(first())

    async def second():
        fresh = SqliteDatabase(str(tmp_path / "queue.db"))
        try:
            claimed = await SqliteDeliveryQueueRepository(fresh).claim_batch(
                worker_id="after-restart", batch_size=5, lease_duration=30)
            assert len(claimed) == 1, claimed
            assert claimed[0].payload_data.get("text") == "survive the restart", claimed[0].payload_data
        finally:
            fresh.close()

    asyncio.run(second())


def test_a_job_claimed_by_a_crashed_worker_is_recoverable_by_the_watchdog(db):
    """The lease is the only thing standing between a crash and a lost message."""
    async def run():
        await seed(db, make_rule("r-11"))
        await enqueue(db, "r-11", 17)
        repo = SqliteDeliveryQueueRepository(db)
        job = (await repo.claim_batch(worker_id="dead-worker", batch_size=5, lease_duration=30))[0]
        db.connection.execute("UPDATE delivery_jobs SET lease_until = ? WHERE id = ?",
                              (time.time() - 5, job.id))
        db.connection.commit()
        recovered = await repo.claim_batch(worker_id="live-worker", batch_size=5, lease_duration=30)
        assert [j.id for j in recovered] == [job.id], recovered
        row = jobs_rows(db)[0]
        assert row["worker_id"] == "live-worker", row
        assert int(row["attempts"]) == 0, row

    asyncio.run(run())


def test_a_job_row_is_a_complete_delivery_instruction(db):
    """Everything the worker needs travels with the job, so a restart can finish it."""
    async def run():
        await seed(db, make_rule("r-12"))
        await enqueue(db, "r-12", 18, text="full instruction")
        claimed = (await SqliteDeliveryQueueRepository(db).claim_batch("w", 5, 30))[0]
        data = claimed.payload_data
        for key in ("source_chat_id", "source_message_id", "target_chat_id", "text", "session_id"):
            assert key in data, (key, sorted(data))
        assert data["target_chat_id"] == TARGET_A, data

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# One mapping row, one owner
# --------------------------------------------------------------------------- #
def test_a_reply_outcome_never_rewrites_the_forwards_mapping(db):
    """The reply shares (rule, source, msg, target) with the forward.

    Writing the reply's outcome there used to replace the forward's
    ``target_message_id`` with the reply's message id — edit and delete sync
    then acted on the reply post instead of the forwarded copy — and rewrote a
    delivered forward's status (a failed reply said the forward was retrying).
    """
    async def run():
        from core.domain.value_objects import DeliveryStatus
        await seed(db, make_rule("r-13"))
        map_repo = SqliteMessageMapRepository(db)
        forward = await enqueue(db, "r-13", 19)
        assert forward is True
        mapping = (await map_repo.get_by_source(SOURCE, 19))[0]
        await map_repo.update_delivery(
            rule_id="r-13", source_chat_id=SOURCE, source_message_id=19,
            target_chat_id=TARGET_A, target_message_id=777,
            status=DeliveryStatus.SENT,
        )
        assert await enqueue(db, "r-13", 19, "the reply", job_kind="keyword_reply") is True

        queue = make_queue(db)
        reply_job = DeliveryJob(
            id="r-13:%s:19:%s:keyword_reply" % (SOURCE, TARGET_A), rule_id="r-13",
            source_chat_id=SOURCE, source_message_id=19, target_chat_id=TARGET_A,
            payload_data={"is_reply": True}, job_kind="keyword_reply",
        )
        await queue._note_mapping(reply_job, DeliveryStatus.FAILED)
        after = (await map_repo.get_by_source(SOURCE, 19))[0]
        assert int(after.target_message_id or 0) == 777, after
        assert str(getattr(after.delivery_status, "value", after.delivery_status)) == "SENT", after

        forward_job = DeliveryJob(
            id=str(mapping.id), rule_id="r-13", source_chat_id=SOURCE,
            source_message_id=19, target_chat_id=TARGET_A, payload_data={},
        )
        await queue._note_mapping(forward_job, DeliveryStatus.RETRY_WAIT)
        updated = (await map_repo.get_by_source(SOURCE, 19))[0]
        assert int(updated.target_message_id or 0) == 777, updated
        assert str(getattr(updated.delivery_status, "value", updated.delivery_status)) == "RETRY_WAIT", updated

    asyncio.run(run())


def test_a_reply_does_not_create_a_mapping_row_of_its_own(db):
    """The mapping is the source→target relationship, owned by the forward."""
    async def run():
        await seed(db, make_rule("r-14"))
        assert await enqueue(db, "r-14", 20, "the reply", job_kind="keyword_reply") is True
        assert await SqliteMessageMapRepository(db).get_by_source(SOURCE, 20) == []
        assert len(jobs_rows(db)) == 1, jobs_rows(db)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# The real pipeline, the real store
# --------------------------------------------------------------------------- #
def test_the_pipeline_stores_both_deliverables_for_a_keyword_match(db):
    """A keyword match queues the forward *and* the reply — through the pipeline.

    The Part 1 test for this feature asserted the *call* carried
    ``job_kind="keyword_reply"`` while stubbing the queue out, so it passed
    while the reply never reached the store. This one runs the pipeline against
    the real database and counts the rows.
    """
    async def run():
        from core.application.pipeline import DurableMessagePipeline

        rule = make_rule("r-15")
        rule.keyword_confirmation = True
        rule.keyword_reply = "keyword matched!"
        rule.whitelist = ["btc"]
        rule.is_active = True
        rule.paid_addons = {"keyword_reply": True}
        await seed(db, rule)

        class Rules:
            async def list_active_by_source(self, chat_id):
                return [rule]

            async def get_by_id(self, rule_id):
                return rule if str(rule_id) == rule.id else None

        class Filters:
            async def get_by_id(self, fid):
                return None

        class Chat:
            id = SOURCE
            type = "channel"
            username = "chan"
            title = "Channel"

        class Msg:
            id = 21
            chat = Chat()
            text = "buy BTC now"
            date = 1700000000
            edit_date = None
            service = None
            photo = video = document = audio = None
            voice = sticker = animation = None
            caption = None
            media_group_id = None

        pipe = DurableMessagePipeline(
            rule_repo=Rules(), filter_repo=Filters(),
            queue_manager=make_queue(db),
            msg_map_repo=SqliteMessageMapRepository(db),
        )
        await pipe.process(Msg())

        rows = {r["job_kind"]: r for r in jobs_rows(db)}
        assert set(rows) == {"forward", "keyword_reply"}, sorted(rows)
        reply_payload = rows["keyword_reply"]["payload_json"]
        assert "keyword matched!" in reply_payload, reply_payload
        assert rows["keyword_reply"]["target_chat_id"] == TARGET_A, rows["keyword_reply"]
        assert rows["forward"]["target_chat_id"] == TARGET_A, rows["forward"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# The worker: both deliverables ship, only the forward is recorded
# --------------------------------------------------------------------------- #
class _RecordingClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, **kwargs):
        class Sent:
            id = 9000 + len(self.sent) + 1
        self.sent.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return Sent()

    async def send_file(self, *args, **kwargs):  # pragma: no cover - not used here
        raise AssertionError("a text-only reply must not upload the source media")

    async def copy_message(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("a text-only reply must not copy the source message")


class _Pool:
    def __init__(self, client) -> None:
        self._client = client

    def get(self, session_id):
        return self._client

    def __getitem__(self, session_id):
        return self._client


def test_the_worker_ships_both_deliverables_and_records_only_the_forward(db):
    """End to end: enqueue a forward and a reply, dispatch both, check the map.

    This is the defect D5 in full: after both deliveries the mapping row must
    still name the *forward's* post, and both jobs must be SENT.
    """
    async def run():
        from core.domain.value_objects import DeliveryStatus

        await seed(db, make_rule("r-16"))
        client = _RecordingClient()
        queue = DurableQueueManager(
            queue_repo=SqliteDeliveryQueueRepository(db),
            message_map_repo=SqliteMessageMapRepository(db),
            client_pool=_Pool(client),
            rule_repo=SqliteForwardRuleRepository(db),
            num_workers=1,
        )
        rule = await SqliteForwardRuleRepository(db).get_by_id("r-16")
        assert await queue.enqueue_delivery(rule, payload(22, "buy BTC now"),
                                           TARGET_A, "buy BTC now") is True
        assert await queue.enqueue_delivery(rule, payload(22, "buy BTC now"),
                                           TARGET_A, "keyword matched!",
                                           job_kind="keyword_reply") is True

        repo = SqliteDeliveryQueueRepository(db)
        for _ in range(4):
            for job in await repo.claim_batch(worker_id="w", batch_size=5, lease_duration=30):
                confirmed = await queue._process_job("w", job)
                if confirmed is not None:
                    await repo.mark_sent(*confirmed)

        assert len(client.sent) == 2, client.sent
        assert {s["text"] for s in client.sent} == {"buy BTC now", "keyword matched!"}, client.sent
        assert {str(s["chat_id"]) for s in client.sent} == {TARGET_A}, client.sent

        mapping = (await SqliteMessageMapRepository(db).get_by_source(SOURCE, 22))[0]
        forward_id = next(i for i, s in enumerate(client.sent, start=9001)
                          if s["text"] == "buy BTC now")
        assert int(mapping.target_message_id or 0) == forward_id, (mapping, client.sent)
        assert str(getattr(mapping.delivery_status, "value", mapping.delivery_status)) == "SENT", mapping

        statuses = {str(r["job_kind"]): r["status"] for r in jobs_rows(db)}
        assert statuses == {"forward": "SENT", "keyword_reply": "SENT"}, statuses
        assert DeliveryStatus.SENT.value == "SENT"  # the status the map is compared against

    asyncio.run(run())
