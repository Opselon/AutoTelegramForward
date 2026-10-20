"""Forward Every N Messages — one value, one counter, one place to argue about it.

What this feature promises an owner, and what these tests pin down:

* a task set to "every 5th message" forwards its 1st, 6th, 11th … accepted
  message and quietly keeps the others;
* only messages that survived every filter are counted, so censored text does
  not make the next legit message wait a slot longer;
* one album is one message, no matter how many photos Telegram packed into it;
* the counter is written as one atomic statement per accepted message, so two
  workers cannot both conclude they own a slot;
* the mechanism is inert until a task configures it: N = 1 forwards everything
  and does not touch the database, which keeps old tasks byte-for-byte alike;
* the sequence survives a restart, a redelivered event and a delivery retry.

The interval edit paths (bot panel, dashboard, mobile) are exercised by the
suites that cover those surfaces; this file owns the engine's semantics.
"""

from __future__ import annotations

# -*- coding: utf-8 -*-

import asyncio
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.application.pipeline import DurableMessagePipeline, PipelineStage
from core.domain import rule_settings
from core.domain.entities import ForwardRule, MessagePayload
from core.domain.forward_interval import (
    MAX_FORWARD_EVERY_N,
    clamp_forward_every_n,
    owns_slot,
    slots_until_next,
)
from core.domain.services import FilterRule
from core.infrastructure.persistence.database import SCHEMA_VERSION, SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryQueueRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
    SqliteRuleCounterRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService

OWNER = 5094837833
SOURCE = "-1004400615270"
TARGET = "-1003915313919"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def make_rule(rule_id: str = "rule-everyn", every_n: int = 1, **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id,
        session_id="sess-a",
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        is_active=True,
        owner_user_id=OWNER,
    )
    rule.duplicate_filters = False
    rule.skip_history = True
    if every_n != 1:
        rule.forward_every_n = every_n
    for key, value in kwargs.items():
        setattr(rule, key, value)
    return rule


def payload(message_id: int, text: str = "hello", **kwargs) -> dict:
    raw = {"chat_id": SOURCE, "message_id": message_id, "text": text, **kwargs}
    return raw


class FakeRuleRepo:
    def __init__(self, rules=None):
        self.rules = list(rules or [])

    async def list_active_by_source(self, source_chat_id):
        return [
            r for r in self.rules
            if str(r.source_chat_id) == str(source_chat_id) and r.is_active
        ]


class FakeFilterRepo:
    async def get_by_id(self, filter_id):
        return None


class RecordingQueue:
    """Only the enqueue half of the durable queue: what was queued for whom."""

    def __init__(self):
        self.jobs: list[tuple[str, int, str]] = []

    async def enqueue_delivery(self, rule, msg, target, text, evaluation=None, **kwargs):
        self.jobs.append((rule.id, msg.message_id, target))
        return True


def ensure_tables(db: SqliteDatabase) -> None:
    """Real schemas, so every test also exercises the migration SQL itself."""
    SqliteSessionRepository(db, CryptoService("test-key"))  # opens on the shared db
    from core.infrastructure.persistence.sqlite_repositories import (
        SqliteUiStateRepository,
    )  # noqa: F401  (table created by the migration list)


def build_pipeline(db, rule_or_rules, counters=None, queue=None, processed=None):
    rules = rule_or_rules if isinstance(rule_or_rules, list) else [rule_or_rules]
    queue = queue or RecordingQueue()
    pipeline = DurableMessagePipeline(
        rule_repo=FakeRuleRepo(rules),
        filter_repo=FakeFilterRepo(),
        queue_manager=queue if queue is not None else RecordingQueue(),
        processed_repo=processed,
        counters=counters,
    )
    return pipeline, queue


# --------------------------------------------------------------------------- #
# The number itself
# --------------------------------------------------------------------------- #
class TestTheIntervalArithmetic:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (1, 1),
            (5, 5),
            (1000, 1000),
            (0, 1),          # 0 would silence the task: it means off
            (-3, 1),
            (1001, 1000),    # capped, not rejected — the documented ceiling
            (10**9, 1000),
            (5.0, 5),
            (5.9, 5),        # an interval is a whole message
            ("5", 5),
            ("  7  ", 7),
            ("", 1),
            ("abc", 1),
            (None, 1),
            (True, 1),
            (False, 1),
            (float("nan"), 1),
            (float("inf"), 1),
            (float("-inf"), 1),
            ({}, 1),
            ([], 1),
            (object(), 1),
        ],
    )
    def test_clamp(self, raw, expected):
        assert clamp_forward_every_n(raw) == expected
        # Any accepted value is idempotent: clamping twice is a no-op.
        assert clamp_forward_every_n(clamp_forward_every_n(raw)) == expected

    def test_n5_forwards_the_first_of_every_cycle(self):
        slots = [c for c in range(1, 21) if owns_slot(c, 5)]
        assert slots == [1, 6, 11, 16]

    def test_one_forwards_every_message(self):
        assert all(owns_slot(c, 1) for c in range(1, 50))

    def test_every_setting_in_range_owns_slots_every_n(self):
        for n in (1, 2, 3, 5, 7, 10, 100, 999, 1000):
            hits = [c for c in range(1, 4 * n + 1) if owns_slot(c, n)]
            assert hits == [1 + k * n for k in range(4)], n

    def test_zero_and_negative_sequences_own_nothing(self):
        # An unreadable counter must not behave like the first message.
        for every_n in (2, 5, 1000):
            assert not owns_slot(0, every_n), every_n
            assert not owns_slot(-17, every_n), every_n
        # N = 1 never looks at the counter at all: every message goes.
        assert owns_slot(0, 1)

    def test_slots_until_next(self):
        assert slots_until_next(1, 5) == 0    # this message goes
        assert slots_until_next(2, 5) == 4    # 3, 4, 5, then 6 goes
        assert slots_until_next(5, 5) == 1    # the very next one goes
        assert slots_until_next(6, 5) == 0
        assert slots_until_next(9, 1) == 0    # N = 1: always now

    def test_the_documented_bounds_are_the_registry_bounds(self):
        spec = rule_settings.SETTING_SPECS["forward_every_n"]
        assert spec.minimum == 1 and spec.maximum == MAX_FORWARD_EVERY_N == 1000
        assert spec.default == 1


# --------------------------------------------------------------------------- #
# The setting on the task
# --------------------------------------------------------------------------- #
class TestTheRuleSetting:
    def test_default_is_off_and_leaves_no_trace(self):
        rule = make_rule()
        assert rule.forward_every_n == 1
        assert "forward_every_n" not in (rule.metadata or {})

    def test_write_then_read_round_trips(self):
        rule = make_rule()
        rule.forward_every_n = 12
        assert rule.forward_every_n == 12
        assert rule.metadata["forward_every_n"] == 12

    def test_writing_the_default_removes_the_key(self):
        rule = make_rule()
        rule.forward_every_n = 12
        rule.forward_every_n = 1
        assert "forward_every_n" not in (rule.metadata or {})

    def test_read_survives_a_hand_edited_row(self):
        # Anything unusable reads as "off" rather than as 0/negative, and an
        # absurd value is capped at the documented ceiling instead of refusing
        # to forward.
        for junk, expected in [
            ({}, 1),
            ({"forward_every_n": None}, 1),
            ({"forward_every_n": "x"}, 1),
            ({"forward_every_n": -1}, 1),
            ({"forward_every_n": "nan"}, 1),
            ({"forward_every_n": 100000}, 1000),
            ("not-a-dict", 1),
        ]:
            rule = make_rule()
            rule.metadata = junk
            assert rule.forward_every_n == expected, junk

    @pytest.mark.parametrize("bad", [0, -1, 1001, 9999999, "nan", "?" ])
    def test_the_registry_rejects_an_out_of_range_value(self, bad):
        rule = make_rule()
        with pytest.raises(rule_settings.SettingError):
            rule_settings.apply_settings(rule, {"forward_every_n": bad})

    def test_the_registry_accepts_and_pins_the_bounds(self):
        rule = make_rule()
        rule_settings.apply_settings(rule, {"forward_every_n": 1000})
        assert rule.forward_every_n == 1000
        assert "forward_every_n" in rule_settings.pinned_keys(rule)

    def test_replace_all_back_to_the_default_clears_the_key(self):
        rule = make_rule()
        rule_settings.apply_settings(rule, {"forward_every_n": 5}, replace_all=True)
        assert rule.metadata["forward_every_n"] == 5
        rule_settings.apply_settings(rule, {"forward_every_n": 1}, replace_all=True)
        assert "forward_every_n" not in (rule.metadata or {})

    def test_clear_setting_returns_to_inherited(self):
        rule = make_rule()
        rule_settings.apply_settings(rule, {"forward_every_n": 4})
        assert rule_settings.clear_setting(rule, "forward_every_n") is True
        assert rule.forward_every_n == 1

    def test_read_settings_reports_it_for_every_task(self):
        out = rule_settings.read_settings(make_rule())
        assert out["forward_every_n"] == 1


# --------------------------------------------------------------------------- #
# The persisted counter
# --------------------------------------------------------------------------- #
class TestThePersistedCounter:
    def test_the_migration_is_in_the_schema(self):
        assert SCHEMA_VERSION >= 27
        db = SqliteDatabase(":memory:")
        try:
            with db._conn as conn:  # the migration has already run
                names = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
            assert "rule_counters" in names
        finally:
            db.close()

    def test_a_sequence_is_one_number_per_accepted_message(self, tmp_path):
        async def body():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                repo = SqliteRuleCounterRepository(db)
                seen = [await repo.advance("r1", 5, OWNER) for _ in range(6)]
                assert seen == [1, 2, 3, 4, 5, 6]
                row = await repo.peek("r1")
                assert row["accepted"] == 6 and row["every_n"] == 5
                assert row["owner_user_id"] == OWNER
            finally:
                db.close()
        asyncio.run(body())

    def test_restarts_do_not_reset_the_count(self, tmp_path):
        async def body():
            path = str(tmp_path / "restart.db")
            db = SqliteDatabase(path)
            try:
                repo = SqliteRuleCounterRepository(db)
                for _ in range(5):
                    await repo.advance("r1", 3, OWNER)
            finally:
                # A restart: process gone, store on disk.
                db.close()
            db = SqliteDatabase(path)
            try:
                repo = SqliteRuleCounterRepository(db)  # fresh object, same file
                assert await repo.advance("r1", 3, OWNER) == 6
            finally:
                db.close()
        asyncio.run(body())

    def test_changing_the_interval_starts_a_fresh_cycle(self, tmp_path):
        async def body():
            db = SqliteDatabase(str(tmp_path / "change.db"))
            try:
                repo = SqliteRuleCounterRepository(db)
                for _ in range(5):
                    await repo.advance("r1", 3, OWNER)
                # The owner re-tunes the cadence; the first message afterwards is
                # message 1 of the new cycle.
                assert await repo.advance("r1", 5, OWNER) == 1
                assert await repo.advance("r1", 5, OWNER) == 2
                # And an owner who goes back to 1 does not spin up a table row…
            finally:
                db.close()
        asyncio.run(body())

    def test_counters_are_scoped_to_one_task(self, tmp_path):
        async def body():
            db = SqliteDatabase(str(tmp_path / "scope.db"))
            try:
                repo = SqliteRuleCounterRepository(db)
                # Two tasks of the same owner, one task of another owner.
                assert await repo.advance("r1", 2, OWNER) == 1
                assert await repo.advance("r1", 2, OWNER) == 2
                assert await repo.advance("r2", 2, OWNER) == 1
                assert await repo.advance("r3", 2, 999) == 1
                assert await repo.advance("r1", 2, OWNER) == 3
            finally:
                db.close()
        asyncio.run(body())

    def test_two_workers_get_two_numbers(self, tmp_path):
        """40 concurrent advances: 40 distinct sequences, no slot shared.

        A read-then-write here would hand the same number to two workers and
        forward the same slot twice; the store must be the only serialisation
        point, and this is the test that would catch any such drift.
        """
        async def body():
            db = SqliteDatabase(str(tmp_path / "race.db"))
            try:
                repo = SqliteRuleCounterRepository(db)
                await repo.advance("r1", 5, OWNER)  # row exists from a previous run

                def one(i):
                    fresh = SqliteRuleCounterRepository(db)
                    return asyncio.run(fresh.advance("r1", 5, OWNER))

                with ThreadPoolExecutor(max_workers=8) as pool:
                    numbers = sorted(int(x) for x in pool.map(one, range(39)))
                numbers = [await repo.advance("r1", 5, OWNER)] + numbers
                numbers.sort()
                # The row existed before the pool, so the 40 numbers the pool
                # and the final advance produced are 2..41 — all distinct.
                assert numbers == list(range(2, 42)), f"missing {set(range(2,42))-set(numbers)}"
            finally:
                db.close()
        asyncio.run(body())

    def test_reset_makes_the_next_message_the_first_of_the_cycle(self, tmp_path):
        async def body():
            db = SqliteDatabase(str(tmp_path / "reset.db"))
            try:
                repo = SqliteRuleCounterRepository(db)
                for _ in range(7):
                    await repo.advance("r1", 5, OWNER)
                assert await repo.reset("r1") is True
                assert await repo.reset("nope") is False
                assert await repo.advance("r1", 5, OWNER) == 1
                assert (await repo.peek("r1"))["accepted"] == 1
            finally:
                db.close()
        asyncio.run(body())


# --------------------------------------------------------------------------- #
# The pipeline gate
# --------------------------------------------------------------------------- #
class TestThePipelineGate:
    async def _enqueue_marks(self, db, rule, queue=None):
        """A pipeline wired to a real store, plus the counters it shares."""
        processed = SqliteProcessedMessageRepository(db)
        counters = SqliteRuleCounterRepository(db)
        pipeline, queue = build_pipeline(db, [rule], counters=counters,
                                         queue=queue, processed=processed)
        return pipeline, queue, counters, processed

    def test_every_fifth_message_goes_through_with_n5(self, tmp_path):
        async def body():
            db = SqliteDatabase(str(tmp_path / "p.db"))
            try:
                rule = make_rule(every_n=5)
                pipeline, queue, counters, processed = await self._enqueue_marks(db, rule)
                enqueued, skipped = [], []
                for message_id in range(1, 13):
                    if message_id % 5 == 1:
                        enqueued.append(message_id)
                    else:
                        skipped.append(message_id)
                    await pipeline.process(payload(message_id, f"m{message_id}"))
                queued_ids = sorted(j[1] for j in queue.jobs)
                assert queued_ids == enqueued, (queued_ids, enqueued)
                row = await counters.peek(rule.id)
                assert row["accepted"] == 12 and row["every_n"] == 5
                assert pipeline.metrics.forward_interval_skipped_total == 9
            finally:
                db.close()
        asyncio.run(body())

    def test_n1_forward_everything_and_writes_no_counter(self, tmp_path):
        async def body():
            db = SqliteDatabase(str(tmp_path / "off.db"))
            try:
                rule = make_rule(every_n=1)
                pipeline, queue, counters, processed = await self._enqueue_marks(db, rule)
                for message_id in (1, 2, 3, 4, 5):
                    ctx = await pipeline.process(payload(message_id))
                    assert any(r.stage == PipelineStage.ENQUEUE and r.success
                               for r in ctx.stage_results)
                assert await counters.peek(rule.id) is None  # inert: no row, no write
            finally:
                db.close()
        asyncio.run(body())

    def test_task_without_the_port_forwards_everything(self, tmp_path):
        async def body():
            db = SqliteDatabase(str(tmp_path / "bare.db"))
            try:
                rule = make_rule(every_n=7)
                pipeline = DurableMessagePipeline(
                    rule_repo=FakeRuleRepo([rule]),
                    filter_repo=FakeFilterRepo(),
                    processed_repo=SqliteProcessedMessageRepository(db),
                    # counters deliberately not wired (an older embed)
                )
                for message_id in (1, 2, 3, 4, 5, 6, 7):
                    ctx = await pipeline.process(payload(message_id))
                    assert any(r.stage == PipelineStage.ENQUEUE and r.success
                               for r in ctx.stage_results)
            finally:
                db.close()
        asyncio.run(body())

    def test_filtered_messages_never_consume_a_slot(self, tmp_path):
        """A message the filter drops is not part of the cadence.

        Sequence: message 1 forwarded (slot 1), then one message the task's own
        policy drops, then the 7 remaining. With N = 3 the second and third
        slots land at 4 and 7 — if the dropped message had counted, 3 would
        have been forwarded one message early.
        """
        async def body():
            db = SqliteDatabase(str(tmp_path / "filter.db"))
            try:
                rule = make_rule(every_n=3, blacklist=["do-not-send"])
                pipeline, queue, counters, processed = await self._enqueue_marks(db, rule)
                await pipeline.process(payload(1, "first"))
                await pipeline.process(payload(2, "please do-not-send this"))
                for message_id in range(3, 9):
                    await pipeline.process(payload(message_id, f"m{message_id}"))
                # Accepted: ids 1, 3, 4, 5, 6, 7, 8 — seven messages, because
                # the censored one never entered the cadence. Slots 1, 4, 7 are
                # therefore ids 1, 5, 8. Had the drop counted as accepted, the
                # batch would have gone 1, 4, 7 (one message early, always).
                queued_ids = sorted(j[1] for j in queue.jobs)
                assert queued_ids == [1, 5, 8], queued_ids
                assert (await counters.peek(rule.id))["accepted"] == 7
            finally:
                db.close()
        asyncio.run(body())

    def test_one_album_is_one_message_even_when_it_has_five_photos(self, tmp_path):
        """An album is aggregated upstream into one payload per media group, so
        the gate must count the payload, not its items."""
        async def body():
            db = SqliteDatabase(str(tmp_path / "album.db"))
            try:
                rule = make_rule(every_n=2)
                pipeline, queue, counters, processed = await self._enqueue_marks(db, rule)
                # Album "A" (3 arrives the same way in production; the payload
                # is the aggregate): occupies slot 1 -> forwarded.
                album_a = {"chat_id": SOURCE, "message_id": 101, "media_group_id": "ga",
                           "photo": {"file_id": "p1"}, "text": "album one"}
                album_b = {"chat_id": SOURCE, "message_id": 102, "media_group_id": "gb",
                           "photo": {"file_id": "p2"}, "caption": "album two"}
                await pipeline.process(album_a)
                await pipeline.process(album_b)
                queued = sorted(j[1] for j in queue.jobs)
                assert queued == [101], queued
                assert (await counters.peek(rule.id))["accepted"] == 2
            finally:
                db.close()
        asyncio.run(body())

    def test_an_edit_never_counts_and_never_uses_a_slot(self, tmp_path):
        async def body():
            db = SqliteDatabase(str(tmp_path / "edit.db"))
            try:
                rule = make_rule(every_n=2)
                pipeline, queue, counters, processed = await self._enqueue_marks(db, rule)
                await pipeline.process({"chat_id": SOURCE, "message_id": 9, "text": "one"})
                # The edit of a message the task has already forwarded.
                await pipeline.process({"chat_id": SOURCE, "message_id": 9, "text": "one*",
                                        "edit_date": 1761300000, "original_date": 1761300000, "is_edit": True})
                # The next real message takes slot 2, i.e. is held.
                ctx = await pipeline.process({"chat_id": SOURCE, "message_id": 10, "text": "two"})
                assert any(r.stage == PipelineStage.FORWARD_INTERVAL and not r.success
                           for r in ctx.stage_results)
                assert (await counters.peek(rule.id))["accepted"] == 2
                # The edit still mirrors: two deliveries, both message 9 (the
                # queue's idempotency key is what collapses them when they
                # land together).
                assert sorted(j[1] for j in queue.jobs) == [9, 9]
            finally:
                db.close()
        asyncio.run(body())

    def test_a_redelivered_message_does_not_advance_the_counter(self, tmp_path):
        """Queue retries and reconnect replays call process() with the same
        event again: the dedupe stage, not the counter, must absorb the repeat."""
        async def body():
            db = SqliteDatabase(str(tmp_path / "replay.db"))
            try:
                rule = make_rule(every_n=2)
                pipeline, queue, counters, processed = await self._enqueue_marks(db, rule)
                for _ in range(3):
                    await pipeline.process(payload(4))  # slot 1
                assert sorted(j[1] for j in queue.jobs) == [4]
                assert (await counters.peek(rule.id))["accepted"] == 1
            finally:
                db.close()
        asyncio.run(body())

    def test_the_cadence_survives_a_warm_restart(self, tmp_path):
        async def body():
            path = str(tmp_path / "warm.db")
            db = SqliteDatabase(path)
            try:
                rule = make_rule(every_n=3)
                pipeline, queue, counters, processed = await self._enqueue_marks(db, rule)
                await pipeline.process(payload(1))   # slot 1 -> goes
                await pipeline.process(payload(2))   # skipped
                assert sorted(j[1] for j in queue.jobs) == [1]
                assert (await counters.peek(rule.id))["accepted"] == 2
            finally:
                db.close()
            # A crash and a restart: cadence continues where it stopped.
            db = SqliteDatabase(path)
            try:
                rule = make_rule(every_n=3)
                pipeline, queue, counters, processed = await self._enqueue_marks(db, rule)
                # The count continued at 2: message 3 is sequence 3, i.e. not
                # slot 4 — a counter that had restarted would forward it.
                ctx = await pipeline.process(payload(3))
                assert any(r.stage == PipelineStage.FORWARD_INTERVAL and not r.success
                           for r in ctx.stage_results)
                assert queue.jobs == []   # a fresh process queued nothing
                await pipeline.process(payload(4))   # sequence 4 = slot
                await pipeline.process(payload(5))   # sequence 5 = held
                assert (await counters.peek(rule.id))["accepted"] == 5
                # The fresh process only queued what it handled: message 4.
                assert sorted(j[1] for j in queue.jobs) == [4]
            finally:
                db.close()
        asyncio.run(body())

    def test_concurrent_messages_produce_exactly_one_slot_winner(self, tmp_path):
        """Ten simultaneous events for one task on N=3: four go, six stay.

        Order-of-arrival decides *which* four, but never the count: the store is
        the serialisation point, so no two workers can both read "next in 3".
        """
        async def body():
            db = SqliteDatabase(str(tmp_path / "conc.db"))
            try:
                rule = make_rule(every_n=3)
                pipeline, queue, counters, processed = await self._enqueue_marks(db, rule)
                results = await asyncio.gather(*[
                    pipeline.process(payload(400 + i)) for i in range(10)
                ])
                assert len(queue.jobs) == 4
                # Slot winners are unique messages, and the pipeline did not
                # crash any of them.
                assert all(any(r.stage == PipelineStage.ENQUEUE and r.success
                               for r in c.stage_results) or
                           any(r.stage == PipelineStage.FORWARD_INTERVAL
                               for r in c.stage_results)
                           for c in results)
                assert (await counters.peek(rule.id))["accepted"] == 10
            finally:
                db.close()
        asyncio.run(body())

    def test_a_broken_counter_forwards_instead_of_silencing_the_task(self, tmp_path):
        class Broken:
            async def advance(self, rule_id, every_n, owner_user_id=0):
                raise RuntimeError("disk full")

        async def body():
            db = SqliteDatabase(str(tmp_path / "broken.db"))
            try:
                rule = make_rule(every_n=5)
                pipeline, queue, counters, processed = await self._enqueue_marks(
                    db, rule, queue=RecordingQueue())
                pipeline._counters = Broken()
                ctx = await pipeline.process(payload(1))
                assert any(r.stage == PipelineStage.ENQUEUE and r.success
                           for r in ctx.stage_results)
            finally:
                db.close()
        asyncio.run(body())

    def test_the_skip_is_recorded_but_not_a_failure(self, tmp_path):
        async def body():
            db = SqliteDatabase(str(tmp_path / "stage.db"))
            try:
                rule = make_rule(every_n=2)
                pipeline, queue, counters, processed = await self._enqueue_marks(db, rule)
                await pipeline.process(payload(1))
                ctx = await pipeline.process(payload(2))
                gate = [r for r in ctx.stage_results if r.stage == PipelineStage.FORWARD_INTERVAL]
                assert len(gate) == 1 and gate[0].success is False
                assert gate[0].reason == "every_n_skip"
                assert ctx.is_failed is False  # a moral decision, not a fault
            finally:
                db.close()
        asyncio.run(body())

    def test_the_queue_handles_a_thousand_message_task(self, tmp_path):
        """N = 1000: message 1 goes; 2..999 wait. 1000 pipeline runs would be a
        slow test, so the workflow is simulated below the gate instead."""
        async def body():
            db = SqliteDatabase(str(tmp_path / "big.db"))
            try:
                rule = make_rule(every_n=1000)
                pipeline, queue, counters, _ = await self._enqueue_marks(db, rule)
                for message_id in (1, 2, 3):
                    ctx = await pipeline.process(payload(message_id))
                    enqueued = any(r.stage == PipelineStage.ENQUEUE and r.success
                                   for r in ctx.stage_results)
                    assert enqueued == (message_id == 1)
                assert (await counters.peek(rule.id))["accepted"] == 3
                # and where the thousandth message sits in its cycle: slots are
                # 1, 1001, 2001 — the first message of every thousand.
                assert owns_slot(1001, 1000) and owns_slot(2001, 1000)
                assert not owns_slot(1000, 1000) and not owns_slot(999, 1000)
            finally:
                db.close()
        asyncio.run(body())
