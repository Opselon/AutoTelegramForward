"""STEER 05 — Duplicate Filters: what a duplicate is, and who earns the claim.

The toggle's promise is "do not forward content this task already forwarded".
Two ways that promise was broken, both measured here through the real
`DurableMessagePipeline` against the real SQLite store:

1. the fingerprint was recorded *before* the message was delivered, so a
   message dropped later (filter, keyword rule, AI drop, backpressure) still
   marked its content as forwarded — the next legitimate post of the same text
   was silently discarded;
2. the check was a read-then-write, so two workers (or two replicas) could both
   read "not seen" and both forward, even though the store has a
   UNIQUE(rule_id, content_hash) index that could decide the winner.

The claim is now atomic and is only earned when the message is accepted for
delivery; anything else gives it back.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import ForwardRule, MessagePayload
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
)

RULE_ID = "rule-dup-1"
RULE_ID_2 = "rule-dup-2"
SOURCE = "-1004400615270"
TARGET = "-1003915313919"
TARGET_2 = "-1003915313920"


class FakeQueue:
    """Records accepted deliveries; can be told to refuse them."""

    def __init__(self, accept: bool = True):
        self.accept = accept
        self.jobs: list[tuple[str, int, str, str]] = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text,
                               evaluation=None, job_kind="forward",
                               correlation_id="", branding=None):
        if not self.accept:
            return False
        self.jobs.append((rule.id, payload.message_id, str(target_chat_id), text))
        return True


class FakePool:
    def get_client(self, *a, **kw):  # pragma: no cover - not reached in these tests
        return None

    async def send_message(self, *a, **kw):  # pragma: no cover
        raise AssertionError("the pipeline must not talk to Telegram directly here")


class FlakyMap:
    """A store whose dedup claim fails; used for the fail-open case."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def claim_content_hash(self, rule_id, content_hash):
        raise RuntimeError("dedup store unavailable")


def make_rule(rule_id: str = RULE_ID, **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id,
        session_id="sess-a",
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        is_active=True,
        owner_user_id=5094837833,
    )
    rule.duplicate_filters = True
    for k, v in kwargs.items():
        setattr(rule, k, v)
    return rule


def build(db, rules, queue, map_repo=None):
    from core.infrastructure.persistence.sqlite_repositories import SqliteFilterRuleRepository

    r_repo = SqliteForwardRuleRepository(db)
    return DurableMessagePipeline(
        rule_repo=r_repo,
        filter_repo=SqliteFilterRuleRepository(db),
        queue_manager=queue,
        msg_map_repo=map_repo or SqliteMessageMapRepository(db),
        processed_repo=SqliteProcessedMessageRepository(db),
    )


async def seed(db, *rules):
    from core.domain.entities import TelegramSession
    from core.infrastructure.persistence.sqlite_repositories import SqliteSessionRepository
    from core.infrastructure.security.crypto import CryptoService

    await SqliteSessionRepository(db, CryptoService("test-key")).add(
        TelegramSession(id="sess-a", phone_number="+10000000000", is_active=True,
                        is_authorized=True, owner_user_id=5094837833)
    )
    repo = SqliteForwardRuleRepository(db)
    for rule in rules:
        await repo.add(rule)


def payload(message_id: int, text: str, chat_id: str = SOURCE) -> MessagePayload:
    return MessagePayload(message_id=message_id, chat_id=chat_id, text=text)


# --------------------------------------------------------------------------- #
# The toggle decides real behaviour
# --------------------------------------------------------------------------- #
class TestTheToggleDecidesRealBehaviour:
    def test_identical_content_is_forwarded_once(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                await seed(db, make_rule())
                queue = FakeQueue()
                pipe = build(db, None, queue)
                await pipe.process(payload(1, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                await pipe.process(payload(2, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                assert [j[1] for j in queue.jobs] == [1], "the second copy must be dropped"
            finally:
                db.close()

        asyncio.run(run())

    def test_the_toggle_off_forwards_both_copies(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                rule = make_rule()
                rule.duplicate_filters = False
                await seed(db, rule)
                queue = FakeQueue()
                pipe = build(db, None, queue)
                await pipe.process(payload(1, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                await pipe.process(payload(2, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                assert [j[1] for j in queue.jobs] == [1, 2]
            finally:
                db.close()

        asyncio.run(run())

    def test_a_different_message_with_different_content_is_not_a_duplicate(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                await seed(db, make_rule())
                queue = FakeQueue()
                pipe = build(db, None, queue)
                await pipe.process(payload(1, "first post"), trigger="NEW_MESSAGE")
                await pipe.process(payload(2, "second, different post"), trigger="NEW_MESSAGE")
                assert [j[1] for j in queue.jobs] == [1, 2]
            finally:
                db.close()

        asyncio.run(run())

    def test_whitespace_and_case_differences_still_count_as_the_same_post(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                await seed(db, make_rule())
                queue = FakeQueue()
                pipe = build(db, None, queue)
                await pipe.process(payload(1, "Big  ANNOUNCEMENT\n\nsale"), trigger="NEW_MESSAGE")
                await pipe.process(payload(2, "big announcement sale"), trigger="NEW_MESSAGE")
                assert [j[1] for j in queue.jobs] == [1], "normalized equal text is a duplicate"
            finally:
                db.close()

        asyncio.run(run())

    def test_another_task_keeps_its_own_copy(self, tmp_path):
        """Dedup is scoped to the task: the same text under another rule ships.

        Both tasks are multi-route so that the established single-match policy
        does not consume the message for the first one only.
        """

        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                first = make_rule()
                second = make_rule(RULE_ID_2, multi_route=True)
                second.target_chat_id = TARGET_2
                first.multi_route = True
                await seed(db, first, second)
                queue = FakeQueue()
                pipe = build(db, None, queue)
                await pipe.process(payload(1, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                assert sorted(j[0] for j in queue.jobs) == sorted([RULE_ID, RULE_ID_2])
            finally:
                db.close()

        asyncio.run(run())

    def test_the_dedupe_memory_survives_a_restart(self, tmp_path):
        """The claim is in the store, not in process memory."""

        async def run():
            path = str(tmp_path / "dup.db")
            db = SqliteDatabase(path)
            await seed(db, make_rule())
            queue = FakeQueue()
            await build(db, None, queue).process(payload(1, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
            db.close()

            db2 = SqliteDatabase(path)          # a fresh process, same store
            try:
                queue2 = FakeQueue()
                await build(db2, None, queue2).process(
                    payload(2, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE"
                )
                assert queue2.jobs == [], "a restart must not forget what was forwarded"
            finally:
                db2.close()

        asyncio.run(run())


# --------------------------------------------------------------------------- #
# A claim is only earned by a delivery
# --------------------------------------------------------------------------- #
class TestTheClaimIsOnlyEarnedByADelivery:
    def test_content_dropped_by_a_filter_does_not_consume_the_claim(self, tmp_path):
        """Regression: the fingerprint used to be written before the filters ran.

        A message the task's own filter rejects never reached the target, so the
        next legitimate post of that text must still be forwarded.
        """

        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                from core.domain.entities import FilterRule
                from core.infrastructure.persistence.sqlite_repositories import (
                    SqliteFilterRuleRepository,
                )

                fr = await SqliteFilterRuleRepository(db).add(
                    FilterRule(name="blk", blacklist_keywords=["free crypto"])
                )
                await seed(db, make_rule(filter_rule_id=fr.id))
                queue = FakeQueue()
                pipe = build(db, None, queue)

                await pipe.process(payload(1, "free crypto giveaway"), trigger="NEW_MESSAGE")
                assert queue.jobs == [], "the filter must drop it"

                # the identical text, now that the filter no longer matches it
                fr.blacklist_keywords = []
                await SqliteFilterRuleRepository(db).update(fr)
                await pipe.process(payload(2, "free crypto giveaway"), trigger="NEW_MESSAGE")
                assert [j[1] for j in queue.jobs] == [2], (
                    "content that was never delivered must not block a later post"
                )
            finally:
                db.close()

        asyncio.run(run())

    def test_a_refused_enqueue_does_not_consume_the_claim(self, tmp_path):
        """Backpressure / a refusing queue: nothing delivered, nothing claimed."""

        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                await seed(db, make_rule())
                queue = FakeQueue(accept=False)
                pipe = build(db, None, queue)
                await pipe.process(payload(1, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                assert queue.jobs == []

                queue.accept = True
                await pipe.process(payload(2, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                assert [j[1] for j in queue.jobs] == [2], (
                    "an undelivered message must not claim the content"
                )
            finally:
                db.close()

        asyncio.run(run())

    def test_a_crash_after_the_claim_gives_it_back(self, tmp_path):
        """An exception mid-processing must not leave the fingerprint claimed."""

        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                await seed(db, make_rule())
                queue = FakeQueue()
                pipe = build(db, None, queue)
                original = pipe._apply_transforms

                async def boom(*a, **kw):
                    raise RuntimeError("transform blew up")

                pipe._apply_transforms = boom
                await pipe.process(payload(1, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                pipe._apply_transforms = original

                await pipe.process(payload(2, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                assert [j[1] for j in queue.jobs] == [2], (
                    "a failed message must not poison the dedup memory"
                )
            finally:
                db.close()

        asyncio.run(run())

    def test_delivered_content_keeps_its_claim(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                await seed(db, make_rule())
                queue = FakeQueue()
                repo = SqliteMessageMapRepository(db)
                pipe = build(db, None, queue, map_repo=repo)
                await pipe.process(payload(1, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                assert len(queue.jobs) == 1
                assert await repo.was_content_forwarded(
                    RULE_ID, pipe._content_fingerprint(payload(1, "SAME ANNOUNCEMENT"))
                ), "a delivered fingerprint must stay in the store"
            finally:
                db.close()

        asyncio.run(run())

    def test_a_dedup_store_failure_fails_open(self, tmp_path):
        """A hiccup in the dedup store must never drop a message."""

        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                await seed(db, make_rule())
                queue = FakeQueue()
                pipe = build(db, None, queue,
                             map_repo=FlakyMap(SqliteMessageMapRepository(db)))
                await pipe.process(payload(1, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                await pipe.process(payload(2, "SAME ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                assert [j[1] for j in queue.jobs] == [1, 2]
            finally:
                db.close()

        asyncio.run(run())


# --------------------------------------------------------------------------- #
# The claim is atomic
# --------------------------------------------------------------------------- #
class TestTheClaimIsAtomic:
    def test_only_one_of_many_concurrent_claims_wins(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                repo = SqliteMessageMapRepository(db)
                results = await asyncio.gather(*[
                    repo.claim_content_hash(RULE_ID, "hash-concurrent") for _ in range(8)
                ])
                assert sum(1 for r in results if r) == 1, (
                    "the unique index must decide a single winner"
                )
                assert await repo.was_content_forwarded(RULE_ID, "hash-concurrent") is True
            finally:
                db.close()

        asyncio.run(run())

    def test_concurrent_identical_messages_are_forwarded_once(self, tmp_path):
        """Two workers, one message: exactly one delivery."""

        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                await seed(db, make_rule())
                queue = FakeQueue()
                pipe = build(db, None, queue)
                await asyncio.gather(*[
                    pipe.process(payload(1, "CONCURRENT ANNOUNCEMENT"), trigger="NEW_MESSAGE")
                    for _ in range(5)
                ])
                assert len(queue.jobs) == 1, f"double delivery: {queue.jobs}"
            finally:
                db.close()

        asyncio.run(run())

    def test_a_released_claim_can_be_taken_again(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                repo = SqliteMessageMapRepository(db)
                assert await repo.claim_content_hash(RULE_ID, "h1") is True
                assert await repo.claim_content_hash(RULE_ID, "h1") is False
                await repo.release_content_hash(RULE_ID, "h1")
                assert await repo.claim_content_hash(RULE_ID, "h1") is True
                # releasing one fingerprint leaves the others alone
                assert await repo.claim_content_hash(RULE_ID, "h2") is True
                await repo.release_content_hash(RULE_ID, "h1")
                assert await repo.was_content_forwarded(RULE_ID, "h2") is True
            finally:
                db.close()

        asyncio.run(run())

    def test_release_is_scoped_to_the_task(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                repo = SqliteMessageMapRepository(db)
                assert await repo.claim_content_hash(RULE_ID, "shared") is True
                assert await repo.claim_content_hash(RULE_ID_2, "shared") is True
                await repo.release_content_hash(RULE_ID, "shared")
                assert await repo.was_content_forwarded(RULE_ID, "shared") is False
                assert await repo.was_content_forwarded(RULE_ID_2, "shared") is True, (
                    "releasing under one task must not free another task's claim"
                )
            finally:
                db.close()

        asyncio.run(run())


# --------------------------------------------------------------------------- #
# The dedup memory expires
# --------------------------------------------------------------------------- #
class TestTheDedupeMemoryExpires:
    def test_fingerprints_older_than_the_window_are_purged_and_fresh_ones_kept(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "dup.db"))
            try:
                repo = SqliteMessageMapRepository(db)
                assert await repo.claim_content_hash(RULE_ID, "old") is True
                assert await repo.claim_content_hash(RULE_ID, "fresh") is True
                db.execute(
                    "UPDATE content_dedup SET created_at = ? WHERE content_hash = ?",
                    (int(time.time()) - 60 * 60 * 24 * 45, "old"),
                )
                cutoff = int(time.time()) - 60 * 60 * 24 * 30
                purged = await repo.purge_content_hashes_all(cutoff)
                assert purged == 1
                assert await repo.was_content_forwarded(RULE_ID, "old") is False
                assert await repo.was_content_forwarded(RULE_ID, "fresh") is True
            finally:
                db.close()

        asyncio.run(run())

    def test_the_hourly_sweep_purges_the_dedupe_memory(self):
        """The retention sweep must actually call the purge (it never did)."""

        from pathlib import Path

        src = Path("core/infrastructure/telegram/durable_queue.py").read_text()
        assert "purge_content_hashes_all(" in src, (
            "content fingerprints must expire with the other retention windows"
        )
        assert "self._map.cleanup()" in src
