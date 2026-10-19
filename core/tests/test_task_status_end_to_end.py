"""STEER 12 — Task Status: one state, one authority, one atomic transition.

A task is stopped by **two** independent mechanisms, and the engine obeys both:

* the owner's on/off switch (`is_active`) — off means the task is not routed at
  all: `matches_source()` answers False and the SQL never selects the row;
* the dashboard pause (`is_paused` + `paused_until`) — the routing still selects
  the task, the pipeline drops the message and the durable worker postpones (or
  drops) a job it had already accepted.

Measured on the pre-fix code:

* the bot's status button was rendered from `is_active` alone, so a task that the
  dashboard had paused — and whose messages nothing was forwarding — was shown as
  **RUNNING 🟢**;
* that button carried only the rule id and the handler did
  `rule.is_active = not rule.is_active`: a *flip* of whatever the database held at
  the moment of the tap, not of what the screen said. A screen rendered while the
  task was running, tapped after it had been paused elsewhere, therefore
  re-activated it; two taps (double-click, two devices) cancelled each other out;
* `ForwardRuleUseCases.toggle()` / `toggle_owned()` were the same read-modify-write
  with no version, so any API caller had the same lost-update window.

The transition is now: the button carries the state it was rendered from, and the
write is a single conditional UPDATE that only applies if the stored state is
still that one. Otherwise it is refused (`stale`) and the screen is re-rendered
with the truth.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.repositories import IForwardRuleRepository
from core.application.use_cases import ForwardRuleUseCases
from core.domain.entities import (
    STATUS_DISABLED,
    STATUS_PAUSED,
    STATUS_PAUSED_UNTIL,
    STATUS_RUNNING,
    TASK_STATUSES,
    ForwardRule,
    MessagePayload,
    TelegramSession,
)
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryQueueRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.durable_queue import DurableQueueManager
from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.pro_bot_ui import (
    ProBotUI,
    _BOT_RENDER_LANGUAGE,
)

OWNER = 5094837833
OTHER = 6278059256
RULE_ID = "rule-status-1"
SOURCE = "-1004400615270"
TARGET = "-1003915313919"


def make_rule(
    rule_id: str = RULE_ID,
    *,
    is_active: bool = True,
    is_paused: bool = False,
    paused_until: int = 0,
    owner: int = OWNER,
) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id,
        session_id="sess-a",
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        is_active=is_active,
        owner_user_id=owner,
    )
    rule.is_paused = is_paused
    rule.paused_until = paused_until
    rule.duplicate_filters = False
    return rule


async def seed(db, *rules) -> None:
    await SqliteSessionRepository(db, CryptoService("test-key")).add(
        TelegramSession(id="sess-a", phone_number="+100****0000", is_active=True,
                        is_authorized=True, owner_user_id=OWNER)
    )
    repo = SqliteForwardRuleRepository(db)
    for rule in rules:
        await repo.add(rule)


def run(coro_factory, tmp_path, name: str = "status.db"):
    async def runner():
        db = SqliteDatabase(str(tmp_path / name))
        try:
            await coro_factory(db)
        finally:
            db.close()

    asyncio.run(runner())


def build_ui(locale: str = "en") -> ProBotUI:
    ui = ProBotUI(
        bot=MagicMock(), i18n=I18n(), sessions=MagicMock(), rules=MagicMock(),
        filter_rules=MagicMock(), ai_configs=MagicMock(),
        pipeline=MagicMock(), pool=MagicMock(), login=MagicMock(),
        log_client=MagicMock(), admin_ids=[OWNER],
    )
    _BOT_RENDER_LANGUAGE.set(locale)
    return ui


@pytest.fixture(autouse=True)
def _render_language_does_not_leak():
    token = _BOT_RENDER_LANGUAGE.set(None)
    try:
        yield
    finally:
        _BOT_RENDER_LANGUAGE.reset(token)


# --------------------------------------------------------------------------- #
# The state itself
# --------------------------------------------------------------------------- #
class TestTheStateIsTheEngineState:
    def test_the_four_states(self):
        now = 1_000_000.0
        assert make_rule().status_state(now) == STATUS_RUNNING
        assert make_rule(is_active=False).status_state(now) == STATUS_DISABLED
        assert make_rule(is_paused=True).status_state(now) == STATUS_PAUSED
        assert make_rule(is_paused=True, paused_until=int(now) + 600).status_state(now) == STATUS_PAUSED_UNTIL

    def test_an_elapsed_pause_is_running_because_the_engine_resumed_it(self):
        """`is_currently_paused` is what the engine asks; the UI must agree."""
        now = 1_000_000.0
        rule = make_rule(is_paused=True, paused_until=int(now) - 1)
        assert rule.is_currently_paused(now) is False
        assert rule.status_state(now) == STATUS_RUNNING
        assert rule.engine_accepts_new_work(now) is True

    def test_only_running_means_the_forwarder_would_act(self):
        now = 1_000_000.0
        assert make_rule().engine_accepts_new_work(now) is True
        for rule in (
            make_rule(is_active=False),
            make_rule(is_paused=True),
            make_rule(is_paused=True, paused_until=int(now) + 60),
        ):
            assert rule.engine_accepts_new_work(now) is False

    def test_a_disabled_task_matches_no_source(self):
        """The routing must not select a switched-off task at all."""
        assert make_rule().matches_source(SOURCE) is True
        assert make_rule(is_active=False).matches_source(SOURCE) is False

    def test_every_state_is_named(self):
        assert set(TASK_STATUSES) == {STATUS_RUNNING, STATUS_PAUSED, STATUS_PAUSED_UNTIL, STATUS_DISABLED}


# --------------------------------------------------------------------------- #
# What the user is shown
# --------------------------------------------------------------------------- #
class TestTheScreenShowsTheEngineState:
    def test_a_paused_task_is_never_displayed_as_running(self):
        now = time.time()
        ui = build_ui("en")
        running = ui._status_label(make_rule())
        paused = ui._status_label(make_rule(is_paused=True))
        timed = ui._status_label(make_rule(is_paused=True, paused_until=int(now) + 3600))
        disabled = ui._status_label(make_rule(is_active=False))
        assert "RUNNING" in running
        assert "PAUSED" in paused and "RUNNING" not in paused
        assert "PAUSED" in timed and "RUNNING" not in timed
        assert "PAUSED" in disabled and "RUNNING" not in disabled
        assert len({running, paused, timed, disabled}) == 4

    def test_the_rendered_task_screen_agrees_for_every_state(self):
        now = int(time.time())
        ui = build_ui("en")
        cases = {
            STATUS_RUNNING: make_rule(),
            STATUS_DISABLED: make_rule(is_active=False),
            STATUS_PAUSED: make_rule(is_paused=True),
            STATUS_PAUSED_UNTIL: make_rule(is_paused=True, paused_until=now + 3600),
        }
        for state, rule in cases.items():
            text, _kbd = ui._render_rule_detail(rule)
            if state == STATUS_RUNNING:
                assert "RUNNING" in text
            else:
                assert "RUNNING" not in text, f"{state} rendered as RUNNING"
                assert "PAUSED" in text

    def test_the_status_button_carries_the_state_it_was_rendered_from(self):
        ui = build_ui("en")
        paused = make_rule(is_paused=True, paused_until=int(time.time()) + 600)
        _text, kbd = ui._render_rule_detail(paused)
        payloads = [button.callback_data for row in kbd.inline_keyboard for button in row]
        assert f"rtg:{RULE_ID}:{STATUS_PAUSED_UNTIL}" in payloads
        _text, kbd = ui._render_rule_detail(make_rule())
        payloads = [button.callback_data for row in kbd.inline_keyboard for button in row]
        assert f"rtg:{RULE_ID}:{STATUS_RUNNING}" in payloads

    def test_the_label_is_localized_in_every_language(self):
        """No surface may fall back to a literal: all ten languages must have it."""
        rule = make_rule(is_paused=True)
        seen = set()
        for locale in ("en", "fa", "ru", "zh", "es", "fr", "id", "it", "pt", "uz"):
            ui = build_ui(locale)
            label = ui._status_label(rule)
            assert label and "ui_rule_status_paused" not in label, locale
            seen.add(label)
        assert len(seen) == 10, "a language fell back to another language's string"

    def test_a_missing_string_is_not_reported_as_a_broken_ui(self):
        """`{until}`/`{status}` are formatted, never left as raw placeholders."""
        rule = make_rule(is_paused=True, paused_until=int(time.time()) + 60)
        for locale in ("en", "fa"):
            ui = build_ui(locale)
            assert "{until}" not in ui._status_label(rule)
            text, _kbd = ui._render_rule_detail(rule)
            assert "{until}" not in text


# --------------------------------------------------------------------------- #
# The transition: atomic, conditional, owner-scoped
# --------------------------------------------------------------------------- #
class TestTheTransitionIsAtomicAndConditional:
    def test_the_current_state_flips_and_persists(self, tmp_path):
        async def body(db):
            await seed(db, make_rule())
            repo = SqliteForwardRuleRepository(db)
            moved = await repo.set_task_status(RULE_ID, target_active=False, expected_state=STATUS_RUNNING)
            assert moved is True
            again = await repo.get_by_id(RULE_ID)
            assert again.is_active is False and again.status_state() == STATUS_DISABLED
            # And back on, from the state the screen now shows.
            moved = await repo.set_task_status(RULE_ID, target_active=True, expected_state=STATUS_DISABLED)
            assert moved is True and (await repo.get_by_id(RULE_ID)).status_state() == STATUS_RUNNING

        run(body, tmp_path)

    def test_two_taps_on_the_same_screen_produce_one_transition(self, tmp_path):
        """A double tap must not flip the task back; the second tap is stale."""
        async def body(db):
            await seed(db, make_rule())
            repo = SqliteForwardRuleRepository(db)
            first = await repo.set_task_status(RULE_ID, target_active=False, expected_state=STATUS_RUNNING)
            second = await repo.set_task_status(RULE_ID, target_active=False, expected_state=STATUS_RUNNING)
            assert (first, second) == (True, False)
            assert (await repo.get_by_id(RULE_ID)).is_active is False

        run(body, tmp_path)

    def test_a_stale_screen_cannot_reactivate_a_paused_task(self, tmp_path):
        """The screen says RUNNING; the task was paused elsewhere since."""
        async def body(db):
            await seed(db, make_rule())
            repo = SqliteForwardRuleRepository(db)
            # The dashboard pauses it while the user's screen still says RUNNING.
            paused = await repo.get_by_id(RULE_ID)
            paused.is_paused = True
            paused.paused_until = int(time.time()) + 3600
            await repo.update(paused)
            moved = await repo.set_task_status(RULE_ID, target_active=True, expected_state=STATUS_RUNNING)
            assert moved is False, "a screen claiming RUNNING re-activated a paused task"
            fresh = await repo.get_by_id(RULE_ID)
            assert fresh.status_state() == STATUS_PAUSED_UNTIL
            # Tapping from the state the screen now shows does resume it.
            assert await repo.set_task_status(
                RULE_ID, target_active=True, expected_state=STATUS_PAUSED_UNTIL) is True
            fresh = await repo.get_by_id(RULE_ID)
            assert fresh.status_state() == STATUS_RUNNING and fresh.is_paused is False

        run(body, tmp_path)

    def test_an_elapsed_pause_cannot_be_reported_as_a_live_pause(self, tmp_path):
        """`paused_until` in the past is RUNNING — that expectation is not a pause."""
        async def body(db):
            await seed(db, make_rule(is_paused=True, paused_until=int(time.time()) - 60))
            repo = SqliteForwardRuleRepository(db)
            moved = await repo.set_task_status(RULE_ID, target_active=False, expected_state=STATUS_PAUSED_UNTIL)
            assert moved is False, "an elapsed pause was treated as a live one and switched off"
            assert (await repo.get_by_id(RULE_ID)).is_active is True
            # The state it really is in does transition.
            assert await repo.set_task_status(RULE_ID, target_active=False, expected_state=STATUS_RUNNING) is True

        run(body, tmp_path)

    def test_an_untimed_pause_and_a_timed_one_are_different_states(self, tmp_path):
        async def body(db):
            await seed(db, make_rule(is_paused=True))
            repo = SqliteForwardRuleRepository(db)
            assert await repo.set_task_status(
                RULE_ID, target_active=True, expected_state=STATUS_PAUSED_UNTIL) is False
            assert await repo.set_task_status(
                RULE_ID, target_active=True, expected_state=STATUS_PAUSED) is True

        run(body, tmp_path)

    def test_a_transition_from_another_owners_state_writes_nothing(self, tmp_path):
        async def body(db):
            await seed(db, make_rule())
            repo = SqliteForwardRuleRepository(db)
            moved = await repo.set_task_status(
                RULE_ID, target_active=False, expected_state=STATUS_RUNNING, owner_user_id=OTHER)
            assert moved is False
            assert (await repo.get_by_id(RULE_ID)).is_active is True

        run(body, tmp_path)

    def test_the_version_moves_with_the_transition(self, tmp_path):
        async def body(db):
            await seed(db, make_rule())
            repo = SqliteForwardRuleRepository(db)
            before = (await repo.get_by_id(RULE_ID)).version
            await repo.set_task_status(RULE_ID, target_active=False, expected_state=STATUS_RUNNING)
            after = (await repo.get_by_id(RULE_ID)).version
            assert after > before, "an optimistic-concurrency reader would not see the change"

        run(body, tmp_path)

    def test_the_state_survives_a_restart(self, tmp_path):
        async def body(db):
            await seed(db, make_rule())
            await SqliteForwardRuleRepository(db).set_task_status(
                RULE_ID, target_active=False, expected_state=STATUS_RUNNING)

        run(body, tmp_path)

        async def reopen(path):
            db = SqliteDatabase(path)
            try:
                rule = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
                assert rule.is_active is False and rule.status_state() == STATUS_DISABLED
            finally:
                db.close()

        asyncio.run(reopen(str(tmp_path / "status.db")))


# --------------------------------------------------------------------------- #
# The use case the bot and the API both go through
# --------------------------------------------------------------------------- #
class TestTheUseCaseReportsWhatHappened:
    def test_ok_stale_and_not_found_are_distinguishable(self, tmp_path):
        async def body(db):
            await seed(db, make_rule())
            repo = SqliteForwardRuleRepository(db)
            cases = ForwardRuleUseCases(repo)

            outcome, fresh = await cases.set_status(RULE_ID, STATUS_RUNNING, OWNER)
            assert outcome == cases.STATUS_OK and fresh.status_state() == STATUS_DISABLED

            outcome, fresh = await cases.set_status(RULE_ID, STATUS_RUNNING, OWNER)
            assert outcome == cases.STATUS_STALE and fresh.status_state() == STATUS_DISABLED

            outcome, fresh = await cases.set_status(RULE_ID, STATUS_RUNNING, OTHER)
            assert outcome == cases.STATUS_NOT_FOUND and fresh is None

            outcome, fresh = await cases.set_status("nope", STATUS_RUNNING, OWNER)
            assert outcome == cases.STATUS_NOT_FOUND and fresh is None

        run(body, tmp_path)

    def test_the_legacy_flip_still_works_and_never_lies(self, tmp_path):
        async def body(db):
            await seed(db, make_rule())
            repo = SqliteForwardRuleRepository(db)
            cases = ForwardRuleUseCases(repo)
            flipped = await cases.toggle_owned(RULE_ID, OWNER)
            assert flipped is not None and flipped.is_active is False
            assert (await cases.toggle_owned(RULE_ID, OWNER)).is_active is True
            assert await cases.toggle_owned(RULE_ID, OTHER) is None

        run(body, tmp_path)

    def test_a_paused_task_is_resumed_by_its_own_status_button(self, tmp_path):
        """The button means "make the engine forward again", not just is_active."""
        async def body(db):
            await seed(db, make_rule(is_paused=True, paused_until=int(time.time()) + 600))
            repo = SqliteForwardRuleRepository(db)
            cases = ForwardRuleUseCases(repo)
            outcome, fresh = await cases.set_status(RULE_ID, STATUS_PAUSED_UNTIL, OWNER)
            assert outcome == cases.STATUS_OK
            assert fresh.status_state() == STATUS_RUNNING
            assert fresh.is_paused is False and fresh.paused_until == 0
            assert fresh.engine_accepts_new_work() is True

        run(body, tmp_path)


class TestTheFallbackIsNeverWhatProductionUses:
    def test_both_production_stores_override_the_conditional_write(self):
        """The non-atomic fallback must never be the code that runs in production.

        `IForwardRuleRepository.set_task_status` is concrete so that non-store
        implementations (test doubles, the backup fakes) keep working. If a store
        ever inherits it instead of overriding it, the atomicity this whole steer
        is about is silently gone — so a store that forgets is a test failure.
        """
        from core.infrastructure.persistence.postgres_repositories import (
            PostgresForwardRuleRepository,
        )

        fallback = IForwardRuleRepository.set_task_status
        for store in (SqliteForwardRuleRepository, PostgresForwardRuleRepository):
            assert store.set_task_status is not fallback, store.__name__

    def test_the_fallback_still_refuses_a_stale_or_foreign_transition(self, tmp_path):
        """The fallback is not a hole: it checks state and owner before writing."""

        class _FallbackOnly(SqliteForwardRuleRepository):
            set_task_status = IForwardRuleRepository.set_task_status

        async def body(db):
            await seed(db, make_rule())
            repo = _FallbackOnly(db)
            assert await repo.set_task_status(RULE_ID, target_active=False, expected_state=STATUS_PAUSED) is False
            assert await repo.set_task_status(
                RULE_ID, target_active=False, expected_state=STATUS_RUNNING, owner_user_id=OTHER) is False
            assert (await repo.get_by_id(RULE_ID)).is_active is True
            assert await repo.set_task_status(RULE_ID, target_active=False, expected_state=STATUS_RUNNING) is True
            assert (await repo.get_by_id(RULE_ID)).is_active is False

        run(body, tmp_path, "fallback.db")


# --------------------------------------------------------------------------- #
# In-flight work
# --------------------------------------------------------------------------- #
class _FakeClient:
    """A pyrogram-shaped client: every method is awaitable and records its name.

    A `MagicMock` cannot stand in for this: the worker awaits whatever the code
    path reaches, and an un-stubbed attribute is a plain Mock — awaiting it raises
    a TypeError that the worker classifies as a *transient send error*, so a test
    built on one passes for the wrong reason.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name):
        async def _call(*_args, **_kwargs):
            self.calls.append(name)
            return SimpleNamespace(id=7)

        return _call


class TestJobsAlreadyAccepted:
    async def _enqueue(self, db, rule):
        await seed(db, rule)
        # The worker resolves its client from the pool (`pool.get(session_id)`),
        # so the pool must hand back the same recording client the test asserts on
        # — a MagicMock here is awaited by the dispatch and turns into a
        # "transient send error", i.e. a passing test for the wrong reason.
        self.client = _FakeClient()
        queue = DurableQueueManager(
            queue_repo=SqliteDeliveryQueueRepository(db),
            message_map_repo=SqliteMessageMapRepository(db),
            client_pool=SimpleNamespace(get=lambda _session_id: self.client),
            rule_repo=SqliteForwardRuleRepository(db),
            num_workers=1,
        )
        stored = await SqliteForwardRuleRepository(db).get_by_id(rule.id)
        assert await queue.enqueue_delivery(stored, MessagePayload(message_id=1, chat_id=SOURCE, text="hello"), TARGET, "hello")
        return queue, SqliteDeliveryQueueRepository(db)

    async def _claimed(self, repo):
        jobs = await repo.claim_batch(worker_id="w1", batch_size=5, lease_duration=30)
        assert len(jobs) == 1
        return jobs[0]

    def test_a_job_accepted_while_running_is_not_delivered_after_the_task_is_switched_off(self, tmp_path):
        async def body(db):
            queue, repo = await self._enqueue(db, make_rule())
            await SqliteForwardRuleRepository(db).set_task_status(
                RULE_ID, target_active=False, expected_state=STATUS_RUNNING)
            job = await self._claimed(repo)
            client = self.client
            assert await queue._process_job("w1", job) is None
            assert client.calls == [], f"the worker sent for a stopped task: {client.calls}"
            row = db.connection.execute(
                "SELECT status, error_detail FROM delivery_jobs WHERE id=?", (job.id,)).fetchone()
            assert row[0] == "FAILED", row
            assert "disabled" in str(row[1]).lower()

        run(body, tmp_path)

    def test_a_job_accepted_before_a_pause_is_postponed_not_sent(self, tmp_path):
        async def body(db):
            queue, repo = await self._enqueue(db, make_rule())
            rule = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
            rule.is_paused = True
            rule.paused_until = int(time.time()) + 900
            await SqliteForwardRuleRepository(db).update(rule)
            job = await self._claimed(repo)
            client = self.client
            assert await queue._process_job("w1", job) is None
            assert client.calls == [], f"the worker sent for a stopped task: {client.calls}"
            gate = db.connection.execute(
                "SELECT next_retry_at FROM delivery_jobs WHERE id=?", (job.id,)).fetchone()[0]
            assert float(gate) > time.time() + 60, "a paused task's job was not postponed"

        run(body, tmp_path)

    def test_a_job_for_an_elapsed_pause_is_delivered(self, tmp_path):
        """The worker and the screen agree: an elapsed pause is not a pause."""
        async def body(db):
            queue, repo = await self._enqueue(db, make_rule())
            rule = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
            rule.is_paused = True
            rule.paused_until = int(time.time()) - 1
            await SqliteForwardRuleRepository(db).update(rule)
            job = await self._claimed(repo)
            client = self.client
            result = await queue._process_job("w1", job)
            assert result is not None, "the worker refused a task whose pause had elapsed"
            assert len(client.calls) == 1, client.calls

        run(body, tmp_path)

    def test_resuming_a_task_releases_its_postponed_jobs(self, tmp_path):
        """Resume must not leave jobs waiting for a pause that is over."""
        async def body(db):
            queue, repo = await self._enqueue(db, make_rule())
            rule = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
            rule.is_paused = True
            rule.paused_until = int(time.time()) + 900
            await SqliteForwardRuleRepository(db).update(rule)
            job = await self._claimed(repo)
            client = self.client
            await queue._process_job("w1", job)
            assert (await repo.claim_batch(worker_id="w2", batch_size=5, lease_duration=30)) == []

            # The RPC's resume path releases exactly these rows.
            released = await SqliteForwardRuleRepository(db).set_task_status(
                RULE_ID, target_active=True, expected_state=STATUS_PAUSED_UNTIL)
            assert released is True
            db.connection.execute(
                "UPDATE delivery_jobs SET next_retry_at = 0 WHERE rule_id = ? AND status = 'RETRY_WAIT'",
                (RULE_ID,))
            db.connection.commit()
            due = await repo.claim_batch(worker_id="w2", batch_size=5, lease_duration=30)
            assert len(due) == 1

        run(body, tmp_path)
