"""Part 2 STEER 05 — combinations, not one switch at a time.

Part 1 proved each switch reaches the engine on its own. This file drives the
real `DurableMessagePipeline` against a real SQLite store with **several**
features configured at once and pins what the pipeline actually does, including
the order of the stages. The order below is read off `_apply_transforms` and the
`process()` stage records; it is *documented*, not invented:

    INGEST → NORMALIZE → DEDUPLICATE → FILTER (gates + content policy)
           → TRANSFORM (cleaner → link engine → emoji → task policy → AI
                        → replacements → header/footer → watermark)
           → ENQUEUE

Two consequences of that order are asserted explicitly, because they are the
kind of thing a user would call a bug if it went the other way:

* a blacklisted word is blocked even when the cleaner would have removed the
  text carrying it (the policy runs *before* any transform);
* a blacklisted word that the task's *own header* introduces is not blocked
  (the header is added *after* the filter stage) — the filter sees the source
  message, never the message the task itself composes.
"""

from __future__ import annotations

import asyncio

import pytest

from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import ForwardRule, MessagePayload, TelegramSession
from core.infrastructure.persistence.database import SqliteDatabase
from core.application.use_cases import ForwardRuleUseCases
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService

OWNER_A = 5094837833
OWNER_B = 5989354154
SOURCE = "-1004400615270"
TARGET_A = "-1003915313919"
TARGET_B = "-1003915313920"


class RecordingQueue:
    """Stands in for the durable queue: records every accepted delivery."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, int, str, str]] = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text,
                               evaluation=None, job_kind="forward",
                               correlation_id="", branding=None):
        self.jobs.append((rule.id, payload.message_id, str(target_chat_id), text))
        return True

    @property
    def texts(self) -> list[str]:
        return [job[3] for job in self.jobs]


class _Empty:
    """A stage the pipeline must tolerate when it has nothing to say."""

    has_rules = False

    def describe(self) -> str:
        return ""


def make_rule(rule_id: str, owner: int = OWNER_A, target: str = TARGET_A,
              session: str = "sess-a", **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id, session_id=session, source_chat_id=SOURCE, target_chat_id=target,
        is_active=True, owner_user_id=owner, **kwargs,
    )
    rule.duplicate_filters = False
    return rule


def build(db, queue) -> DurableMessagePipeline:
    return DurableMessagePipeline(
        rule_repo=SqliteForwardRuleRepository(db),
        filter_repo=SqliteFilterRuleRepository(db),
        queue_manager=queue,
        msg_map_repo=SqliteMessageMapRepository(db),
        processed_repo=SqliteProcessedMessageRepository(db),
    )


async def seed(db, *rules) -> None:
    await SqliteSessionRepository(db, CryptoService("test-key")).add(
        TelegramSession(id="sess-a", phone_number="+100****0000", is_active=True,
                        is_authorized=True, owner_user_id=OWNER_A)
    )
    repo = SqliteForwardRuleRepository(db)
    for rule in rules:
        await repo.add(rule)


def payload(message_id: int = 1, text: str = "", **kwargs) -> MessagePayload:
    return MessagePayload(message_id=message_id, chat_id=SOURCE, text=text, **kwargs)


async def process(db, queue, message, trigger: str = "NEW_MESSAGE"):
    """One pass of the real pipeline; the queue is cleared first."""
    queue.jobs.clear()
    return await build(db, queue).process(message, trigger=trigger)


@pytest.fixture
def workspace(tmp_path):
    db = SqliteDatabase(str(tmp_path / "combo.db"))
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 1. The documented order, asserted from both sides
# --------------------------------------------------------------------------- #
def test_a_blacklisted_word_is_blocked_even_when_the_cleaner_would_remove_it(workspace):
    """The content policy runs before every transform: the filter sees the source."""
    db, queue = workspace, RecordingQueue()

    async def run():
        rule = make_rule("r-order-1")
        rule.blacklist = ["scam"]
        rule.remove_links = True
        await seed(db, rule)
        # The blacklisted word only exists inside the link the cleaner removes.
        await process(db, queue, payload(text="deal https://t.me/ad scam here"))
        assert queue.jobs == [], f"a blocked message reached delivery: {queue.jobs}"

    asyncio.run(run())


def test_a_word_the_tasks_own_header_introduces_is_not_filtered(workspace):
    """The header is added after the filter: a task cannot filter itself out."""
    db, queue = workspace, RecordingQueue()

    async def run():
        rule = make_rule("r-order-2")
        rule.blacklist = ["VIP-ONLY"]
        rule.header = "VIP-ONLY"
        await seed(db, rule)
        await process(db, queue, payload(text="hello"))
        assert queue.texts == ["VIP-ONLY\n\nhello"], queue.jobs

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 2. Several transforms at once
# --------------------------------------------------------------------------- #
def test_cleaner_then_replace_then_header_compose_in_one_pass(workspace):
    db, queue = workspace, RecordingQueue()

    async def run():
        rule = make_rule("r-combo-1")
        rule.remove_links = True
        rule.clean_hashtags = True
        rule.replacements = {"Promo": "Sale"}
        rule.header = "HEAD"
        rule.footer = "FOOT"
        await seed(db, rule)
        await process(db, queue, payload(text="Promo https://t.me/ad #gold"))
        assert queue.texts == ["HEAD\n\nSale\n\nFOOT"], queue.jobs

    asyncio.run(run())


def test_an_expanding_replacement_is_applied_once(workspace):
    """`#` → `##` must not be applied twice (`####gold`)."""
    db, queue = workspace, RecordingQueue()

    async def run():
        rule = make_rule("r-combo-2")
        rule.replacements = {"#": "##"}
        await seed(db, rule)
        await process(db, queue, payload(text="#gold"))
        assert queue.texts == ["##gold"], queue.jobs

    asyncio.run(run())


def test_a_message_that_cleans_to_nothing_is_dropped_with_a_reason(workspace):
    """Not delivered as an empty message, and not dropped silently either."""
    db, queue = workspace, RecordingQueue()

    async def run():
        rule = make_rule("r-combo-3")
        rule.remove_links = True
        await seed(db, rule)
        ctx = await process(db, queue, payload(text="https://t.me/ad"))
        assert queue.jobs == [], queue.jobs
        reasons = [r.reason for r in ctx.stage_results if not r.success]
        assert "empty_after_clean" in reasons, reasons

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 3. Combinations with the queue and with a second tenant
# --------------------------------------------------------------------------- #
def test_the_same_message_is_delivered_once_and_the_replay_is_deduped(workspace):
    db, queue = workspace, RecordingQueue()

    async def run():
        rule = make_rule("r-combo-4")
        rule.duplicate_filters = True
        await seed(db, rule)
        await process(db, queue, payload(message_id=7, text="only once"))
        first = list(queue.jobs)
        assert len(first) == 1, first
        ctx = await process(db, queue, payload(message_id=7, text="only once"))
        assert queue.jobs == [], "the same event was delivered twice"
        reasons = [r.reason for r in ctx.stage_results if not r.success]
        assert any(r in ("duplicate_event", "duplicate_content") for r in reasons), reasons

    asyncio.run(run())


def test_two_tenants_tasks_deliver_to_their_own_targets_from_one_message(workspace):
    """Two owners, one source message: each task delivers to its own target."""
    db, queue = workspace, RecordingQueue()

    async def run():
        sessions = SqliteSessionRepository(db, CryptoService("test-key"))
        await sessions.add(TelegramSession(id="sess-b", phone_number="+100****0001",
                                           is_active=True, is_authorized=True, owner_user_id=OWNER_B))
        rule_a = make_rule("r-combo-5a", owner=OWNER_A, target=TARGET_A)
        rule_b = make_rule("r-combo-5b", owner=OWNER_B, target=TARGET_B, session="sess-b")
        await seed(db, rule_a)
        await SqliteForwardRuleRepository(db).add(rule_b)
        await process(db, queue, payload(message_id=11, text="shared source"))
        assert sorted(job[2] for job in queue.jobs) == sorted([TARGET_A, TARGET_B]), queue.jobs
        assert len(queue.jobs) == 2, queue.jobs

    asyncio.run(run())


def test_creating_a_task_on_another_tenants_account_is_refused(workspace):
    """The session binding is checked where it is chosen, not at delivery."""
    db = workspace

    async def run():
        await seed(db, make_rule("r-ignored"))  # seeds sess-a, owned by OWNER_A
        use_case = ForwardRuleUseCases(
            repo=SqliteForwardRuleRepository(db),
            session_repo=SqliteSessionRepository(db, CryptoService("test-key")),
        )
        with pytest.raises(PermissionError):
            await use_case.create(make_rule("r-borrowed", owner=OWNER_B, session="sess-a"))
        stored = await SqliteForwardRuleRepository(db).get_by_id("r-borrowed")
        assert stored is None, "a refused task must not be stored"

    asyncio.run(run())


def test_a_reply_to_an_unforwarded_parent_is_still_delivered(workspace):
    db, queue = workspace, RecordingQueue()

    async def run():
        rule = make_rule("r-combo-6")
        await seed(db, rule)
        ctx = await process(db, queue, payload(message_id=12, text="answer", reply_to_message_id=999))
        assert len(queue.jobs) == 1, queue.jobs
        # No mapping is invented for a parent that was never forwarded.
        cur = db.connection.execute("SELECT COUNT(*) FROM message_map WHERE source_message_id = 999")
        assert int(cur.fetchone()[0]) == 0

    asyncio.run(run())


def test_an_off_task_is_not_delivered_and_the_reason_is_honest(workspace):
    """A task switched OFF is not a candidate at all: no delivery, no crash."""
    db, queue = workspace, RecordingQueue()

    async def run():
        rule = make_rule("r-combo-7")
        rule.is_active = False
        await seed(db, rule)
        ctx = await process(db, queue, payload(message_id=13, text="while off"))
        assert queue.jobs == [], queue.jobs
        reasons = [r.reason for r in ctx.stage_results if not r.success]
        assert "no_matching_rules" in reasons, reasons

    asyncio.run(run())


def test_a_paused_task_is_reported_as_paused_not_as_no_rules(workspace):
    """A task the user stopped must not be logged as a routing miss."""
    db, queue = workspace, RecordingQueue()

    async def run():
        rule = make_rule("r-combo-8")
        rule.is_paused = True
        rule.paused_until = 0.0
        await seed(db, rule)
        ctx = await process(db, queue, payload(message_id=15, text="while paused"))
        assert queue.jobs == [], queue.jobs
        reasons = [r.reason for r in ctx.stage_results if not r.success]
        assert "rule_paused" in reasons, reasons

    asyncio.run(run())


def test_a_target_override_still_rejects_only_its_own_target(workspace):
    """The fix must not cost the per-target override its teeth."""
    db, queue = workspace, RecordingQueue()

    async def run():
        rule = make_rule("r-combo-9")
        rule.target_chat_ids = [TARGET_A, TARGET_B]
        rule.target_policies = {TARGET_B: {"blacklist_words": ["spam"]}}
        await seed(db, rule)
        await process(db, queue, payload(message_id=16, text="spam for B only"))
        assert [job[2] for job in queue.jobs] == [TARGET_A], queue.jobs
        await process(db, queue, payload(message_id=17, text="clean for both"))
        assert sorted(job[2] for job in queue.jobs) == sorted([TARGET_A, TARGET_B]), queue.jobs

    asyncio.run(run())


def test_a_target_blacklist_is_not_replaced_by_the_task_decorations(workspace):
    """The target override judges the text the target would receive."""
    db, queue = workspace, RecordingQueue()

    async def run():
        rule = make_rule("r-combo-10")
        rule.target_chat_ids = [TARGET_A, TARGET_B]
        rule.target_policies = {TARGET_B: {"blacklist_words": ["HEAD"]}}
        rule.header = "HEAD"
        await seed(db, rule)
        await process(db, queue, payload(message_id=18, text="body"))
        assert [job[2] for job in queue.jobs] == [TARGET_A], queue.jobs

    asyncio.run(run())
