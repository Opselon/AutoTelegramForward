"""STEER 11 — Set Delay: one validated value, applied once, durably.

The task delay is configured from the bot's Delay screen (`rdly_s:<rule_id>:<sec>`
presets plus a free-text step) and from the advanced settings' dispatch delay
(`forward_delay_seconds`). `AdvancedSettingsEngine.get_dispatch_delay` is the
single authority that resolves the two, and it is read by both dispatch paths.

Measured on the pre-fix code:

* the preset callback did `rule.delay_seconds = float(sec_str)` with no
  validation, so a crafted callback could store `-600`, `inf` or `1e18`; `nan`
  was worse — SQLite kept NULL and the write raised `NOT NULL constraint failed:
  forward_rules.delay_seconds`, i.e. a client could make the save blow up;
* `inf` then travelled into the job payload, where `json.dumps` writes
  `Infinity` — a token strict JSON parsers (Go's `encoding/json`, `JSON.parse`)
  reject, so the whole job row became unreadable to the API;
* the durable path applied the delay **twice**: the job's claim gate was set to
  `now + rule.delay_seconds` while the worker, after claiming, slept
  `min(advanced_delay_seconds, 3600)` — the same number for a rule whose advanced
  delay is unset. A configured 300 s wait became 600 s, and the in-worker sleep
  died with the process while the gate survived;
* the layers disagreed about the maximum: the advanced schema advertises 86400 s,
  while `forward_delay_seconds` silently truncated to 3600 s.

These tests pin the fixed behaviour against the real store and the real worker
dispatch path.
"""

from __future__ import annotations

import asyncio
import json
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import core.infrastructure.telegram.durable_queue as dq
from core.domain.entities import (
    MAX_FORWARD_DELAY_SECONDS,
    ForwardRule,
    MessagePayload,
    TelegramSession,
    clamp_delay_seconds,
)
from core.domain.services import AdvancedSettingsEngine
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryQueueRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.durable_queue import DurableQueueManager

OWNER = 5094837833
RULE_ID = "rule-delay-1"
SOURCE = "-1004400615270"
TARGET = "-1003915313919"


def make_rule(rule_id: str = RULE_ID, delay: float = 0.0, **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id,
        session_id="sess-a",
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        is_active=True,
        owner_user_id=OWNER,
        delay_seconds=delay,
    )
    rule.duplicate_filters = False
    for key, value in kwargs.items():
        setattr(rule, key, value)
    return rule


async def seed(db, *rules) -> None:
    await SqliteSessionRepository(db, CryptoService("test-key")).add(
        TelegramSession(id="sess-a", phone_number="+100****0000", is_active=True,
                        is_authorized=True, owner_user_id=OWNER)
    )
    repo = SqliteForwardRuleRepository(db)
    for rule in rules:
        await repo.add(rule)


def payload(message_id: int = 1) -> MessagePayload:
    return MessagePayload(message_id=message_id, chat_id=SOURCE, text="hello")


class _AsyncioSpy:
    """Real asyncio, with every `sleep` recorded and collapsed to zero."""

    def __init__(self, real):
        self._real = real
        self.sleeps: list[float] = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def sleep(self, delay=0, *args, **kwargs):
        self.sleeps.append(float(delay))
        return await self._real.sleep(0)

    def delay_sleeps(self, minimum: float) -> list[float]:
        return [s for s in self.sleeps if s >= minimum]


def run(coro_factory, tmp_path, name: str = "delay.db"):
    async def runner():
        db = SqliteDatabase(str(tmp_path / name))
        try:
            await coro_factory(db)
        finally:
            db.close()

    asyncio.run(runner())


# --------------------------------------------------------------------------- #
# What a client is allowed to store
# --------------------------------------------------------------------------- #
class TestTheDelayValueIsValidatedWhereverItEnters:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("0", 0.0),
            ("3", 3.0),
            ("3.5", 3.5),
            ("86400", 86400.0),
            ("86400.5", 86400.0),      # capped at the documented maximum
            ("1e18", 86400.0),         # no unbounded value survives
            ("-600", 0.0),             # the preset callback used to store this
            ("nan", 0.0),              # used to raise NOT NULL on save
            ("inf", 0.0),              # used to write Infinity into the payload
            ("-inf", 0.0),
            ("1e400", 0.0),            # overflow -> inf
            ("abc", 0.0),
            ("", 0.0),
            ("  7  ", 7.0),
            (None, 0.0),
            (float("nan"), 0.0),
            (float("inf"), 0.0),
            (0, 0.0),
            (12, 12.0),
        ],
    )
    def test_only_a_finite_in_range_number_survives(self, raw, expected):
        assert clamp_delay_seconds(raw) == expected

    def test_construction_normalizes_the_first_class_field(self):
        assert ForwardRule(id="a", delay_seconds=float("nan")).delay_seconds == 0.0
        assert ForwardRule(id="b", delay_seconds=-30).delay_seconds == 0.0
        assert ForwardRule(id="c", delay_seconds=1e9).delay_seconds == MAX_FORWARD_DELAY_SECONDS
        assert ForwardRule(id="d", delay_seconds="45").delay_seconds == 45.0

    def test_the_advanced_setting_uses_the_same_maximum_as_the_schema(self):
        """The schema advertises 86400 s; the property must not truncate to 3600."""
        rule = make_rule()
        rule.forward_delay_seconds = 43200
        assert rule.forward_delay_seconds == 43200.0
        rule.forward_delay_seconds = 90000
        assert rule.forward_delay_seconds == MAX_FORWARD_DELAY_SECONDS == 86400.0
        rule.forward_delay_seconds = float("inf")
        assert rule.forward_delay_seconds == 0.0

    def test_a_crafted_preset_payload_cannot_break_the_save(self, tmp_path):
        """`rdly_s:<id>:nan` reached SQLite as NULL and raised IntegrityError."""
        async def body(db):
            await seed(db, make_rule())
            repo = SqliteForwardRuleRepository(db)
            rule = await repo.get_by_id(RULE_ID)
            for crafted in ("nan", "inf", "-600", "1e400", "abc"):
                rule.delay_seconds = clamp_delay_seconds(crafted)  # what the handler does now
                await repo.update(rule)
                stored = await repo.get_by_id(RULE_ID)
                assert stored.delay_seconds == 0.0, crafted

        run(body, tmp_path)

    def test_the_typed_step_still_reports_a_typo_but_clamps_a_number(self, tmp_path):
        """`abc` stays an error; an out-of-range number is clamped, not rejected."""
        async def body(db):
            await seed(db, make_rule())
            from core.infrastructure.telegram.pro_bot_ui import ProBotUI

            rule = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
            replies: list[str] = []

            class _Msg:
                from_user = types.SimpleNamespace(id=OWNER)

                async def reply_text(self, text, **_kwargs):
                    replies.append(text)

            ui = ProBotUI.__new__(ProBotUI)
            ui._t = lambda key, **kw: key
            ui._lang_for = lambda r: "en"
            ui._main_menu = lambda: None
            ui._render_delay_menu = lambda r: ("menu", None)

            async def _owned(rule_id, uid):
                assert uid == OWNER
                return rule

            async def _save(r, uid):
                await SqliteForwardRuleRepository(db).update(r)

            async def _persist(uid, state):
                return None

            ui._owned_rule = _owned
            ui._save_owned_rule = _save
            ui._persist = _persist

            state = types.SimpleNamespace(step="set_delay_" + RULE_ID)
            await ui._step_set_delay(_Msg(), state, "abc", RULE_ID)
            assert replies[-1] == "ui_step_delay_invalid_error"
            assert (await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)).delay_seconds == 0.0

            await ui._step_set_delay(_Msg(), state, "999999", RULE_ID)
            stored = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
            assert stored.delay_seconds == MAX_FORWARD_DELAY_SECONDS

        run(body, tmp_path)


# --------------------------------------------------------------------------- #
# Persistence, ownership and per-task scope
# --------------------------------------------------------------------------- #
class TestTheDelayIsPersistedPerTask:
    def test_each_task_keeps_its_own_delay(self, tmp_path):
        async def body(db):
            await seed(db, make_rule(RULE_ID, delay=300.0), make_rule("rule-delay-2", delay=15.0))
            repo = SqliteForwardRuleRepository(db)
            assert (await repo.get_by_id(RULE_ID)).delay_seconds == 300.0
            assert (await repo.get_by_id("rule-delay-2")).delay_seconds == 15.0

        run(body, tmp_path)

    def test_the_delay_survives_a_reopen(self, tmp_path):
        async def body(db):
            await seed(db, make_rule(delay=42.0))

        run(body, tmp_path)
        # Fresh connection: the value is read back from the store, not from memory.
        async def reopen(path):
            db = SqliteDatabase(path)
            try:
                rule = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
                assert rule.delay_seconds == 42.0
                assert AdvancedSettingsEngine.get_dispatch_delay(rule) == 42.0
            finally:
                db.close()

        asyncio.run(reopen(str(tmp_path / "delay.db")))

    def test_a_delay_belonging_to_one_task_never_gates_another(self, tmp_path):
        async def body(db):
            slow = make_rule(RULE_ID, delay=3600.0)
            slow.target_chat_id = "-1001111111111"
            fast = make_rule("rule-delay-2")
            fast.target_chat_id = "-1002222222222"
            await seed(db, slow, fast)
            queue_repo = SqliteDeliveryQueueRepository(db)
            queue = DurableQueueManager(
                queue_repo=queue_repo,
                message_map_repo=SqliteMessageMapRepository(db),
                client_pool=MagicMock(),
                rule_repo=SqliteForwardRuleRepository(db),
                num_workers=1,
            )
            repo = SqliteForwardRuleRepository(db)
            await queue.enqueue_delivery(await repo.get_by_id(RULE_ID), payload(1), slow.target_chat_id, "slow")
            await queue.enqueue_delivery(await repo.get_by_id("rule-delay-2"), payload(2), fast.target_chat_id, "fast")
            due = await queue_repo.claim_batch(worker_id="w1", batch_size=10, lease_duration=30)
            assert [job.rule_id for job in due] == ["rule-delay-2"]

        run(body, tmp_path)

    def test_one_destination_is_one_ordered_lane(self, tmp_path):
        """Two tasks writing to the same target keep that destination in order.

        A destination is deliberately a single lane (one in-flight job per target),
        so an earlier, still-waiting job for the same target holds the lane until its
        delay elapses. Recorded as intended behaviour, not as a cross-task leak: the
        later job keeps its own configuration and its own gate.
        """
        async def body(db):
            await seed(db, make_rule(RULE_ID, delay=3600.0), make_rule("rule-delay-2"))
            queue_repo = SqliteDeliveryQueueRepository(db)
            queue = DurableQueueManager(
                queue_repo=queue_repo,
                message_map_repo=SqliteMessageMapRepository(db),
                client_pool=MagicMock(),
                rule_repo=SqliteForwardRuleRepository(db),
                num_workers=1,
            )
            repo = SqliteForwardRuleRepository(db)
            await queue.enqueue_delivery(await repo.get_by_id(RULE_ID), payload(1), TARGET, "slow")
            await queue.enqueue_delivery(await repo.get_by_id("rule-delay-2"), payload(2), TARGET, "fast")
            assert await queue_repo.claim_batch(worker_id="w1", batch_size=10, lease_duration=30) == []
            # Once the earlier job's gate has elapsed it is claimable, in order.
            db.connection.execute("UPDATE delivery_jobs SET next_retry_at = 0")
            db.connection.commit()
            due = await queue_repo.claim_batch(worker_id="w1", batch_size=10, lease_duration=30)
            assert [job.rule_id for job in due] == [RULE_ID]

        run(body, tmp_path)


# --------------------------------------------------------------------------- #
# Enforcement: once, by the durable gate
# --------------------------------------------------------------------------- #
class TestTheDelayIsEnforcedOnceByTheDurableGate:
    async def _enqueue(self, db, delay: float, message_id: int = 1, **kwargs):
        await seed(db, make_rule(delay=delay, **kwargs))
        queue = DurableQueueManager(
            queue_repo=SqliteDeliveryQueueRepository(db),
            message_map_repo=SqliteMessageMapRepository(db),
            client_pool=MagicMock(),
            rule_repo=SqliteForwardRuleRepository(db),
            num_workers=1,
        )
        rule = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
        before = time.time()
        assert await queue.enqueue_delivery(rule, payload(message_id), TARGET, "hello")
        row = db.connection.execute(
            "SELECT next_retry_at, payload_json FROM delivery_jobs WHERE rule_id=? ORDER BY id DESC LIMIT 1",
            (RULE_ID,),
        ).fetchone()
        return queue, float(row[0]), json.loads(row[1]), before

    def test_the_gate_carries_the_configured_delay(self, tmp_path):
        async def body(db):
            _queue, gate, data, before = await self._enqueue(db, delay=8.0)
            assert abs(gate - (before + 8.0)) < 2.0, gate - before
            assert data["delay_seconds"] == 8.0
            assert data["advanced_delay_seconds"] == 8.0

        run(body, tmp_path)

    def test_the_advanced_setting_wins_the_gate(self, tmp_path):
        """One authority: the advanced delay is what the job is scheduled with."""
        async def body(db):
            _queue, gate, data, before = await self._enqueue(
                db, delay=300.0, metadata={"forward_delay_seconds": 7.0}
            )
            assert abs(gate - (before + 7.0)) < 2.0, gate - before
            assert data["advanced_delay_seconds"] == 7.0

        run(body, tmp_path)

    def test_zero_delay_is_instantly_claimable(self, tmp_path):
        async def body(db):
            _queue, gate, data, _before = await self._enqueue(db, delay=0.0)
            assert gate == 0.0
            assert data["advanced_delay_seconds"] == 0.0
            due = await SqliteDeliveryQueueRepository(db).claim_batch(
                worker_id="w1", batch_size=10, lease_duration=30)
            assert len(due) == 1

        run(body, tmp_path)

    def test_a_restarted_worker_still_respects_the_delay(self, tmp_path):
        """The gate is in the database, so a restart cannot deliver early."""
        async def first(db):
            await self._enqueue(db, delay=600.0)

        run(first, tmp_path)

        async def second(path):
            db = SqliteDatabase(path)
            try:
                claimable = await SqliteDeliveryQueueRepository(db).claim_batch(
                    worker_id="w-after-restart", batch_size=10, lease_duration=30)
                assert claimable == [], "a restarted worker delivered a delayed job early"
            finally:
                db.close()

        asyncio.run(second(str(tmp_path / "delay.db")))

    def test_dispatch_does_not_sleep_for_a_delay_the_gate_already_waited(self, tmp_path):
        """The double application: gate + in-worker sleep made 8 s into 16 s.

        The gate is moved into the past — exactly what elapsing looks like — and
        the real dispatch is driven with a real pyrogram-shaped client. Any sleep
        of the configured delay during that dispatch is the defect.
        """
        async def body(db):
            delay_value = 8.0
            queue, _gate, _data, _before = await self._enqueue(db, delay=delay_value)
            db.connection.execute("UPDATE delivery_jobs SET next_retry_at = 0")
            db.connection.commit()
            jobs = await SqliteDeliveryQueueRepository(db).claim_batch(
                worker_id="w1", batch_size=5, lease_duration=30)
            assert len(jobs) == 1

            client = MagicMock()
            sent = types.SimpleNamespace(id=7)
            for name in ("copy_message", "send_message", "forward_messages", "send_media_group"):
                setattr(client, name, AsyncMock(return_value=sent))

            real_asyncio = dq.asyncio
            spy = _AsyncioSpy(real_asyncio)
            dq.asyncio = spy
            try:
                message_id = await queue._dispatch_to_telegram(client, jobs[0])
            finally:
                dq.asyncio = real_asyncio

            assert message_id == 7
            assert spy.delay_sleeps(delay_value) == [], (
                f"the worker slept for the delay the gate had already honoured: {spy.sleeps}"
            )

        run(body, tmp_path)


# --------------------------------------------------------------------------- #
# The job payload stays valid JSON for the Go API
# --------------------------------------------------------------------------- #
class TestTheJobPayloadIsAlwaysStrictJson:
    def test_a_validated_delay_never_puts_nan_or_infinity_in_a_job(self, tmp_path):
        async def body(db):
            rule = make_rule(delay=clamp_delay_seconds("inf"))
            await seed(db, rule)
            rule = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
            queue = DurableQueueManager(
                queue_repo=SqliteDeliveryQueueRepository(db),
                message_map_repo=SqliteMessageMapRepository(db),
                client_pool=MagicMock(),
                rule_repo=SqliteForwardRuleRepository(db),
                num_workers=1,
            )
            await queue.enqueue_delivery(rule, payload(), TARGET, "hello")
            raw = db.connection.execute(
                "SELECT payload_json FROM delivery_jobs LIMIT 1").fetchone()[0]

            def _refuse(token):
                raise ValueError(f"invalid JSON token from Python: {token}")

            # A strict parser (Go's encoding/json, JS JSON.parse) rejects these.
            decoded = json.loads(raw, parse_constant=_refuse)
            assert decoded["delay_seconds"] == 0.0
            assert "NaN" not in raw and "Infinity" not in raw

        run(body, tmp_path)


def test_the_schema_and_the_property_agree_on_the_maximum():
    """A setting the UI advertises must be a delay the engine honours."""
    import core.domain.rule_settings as rs

    schema_max = rs.SETTING_SPECS["forward_delay_seconds"].maximum
    assert schema_max == int(MAX_FORWARD_DELAY_SECONDS)


def test_the_constant_is_where_the_tests_say_it_is():
    assert Path("core/domain/entities.py").exists() or Path(
        __file__).resolve().parents[1].joinpath("domain/entities.py").exists()
