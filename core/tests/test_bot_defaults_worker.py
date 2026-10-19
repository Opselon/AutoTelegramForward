"""A bot's defaults must reach the forwarder, and must never be written back.

The API tests stop at "the settings endpoint reports the effective value". These
start where the message is: they drive the real pipeline with a real rule and a
real bot token, and check that

  * the transform uses the inherited value (a bot's "remove emojis" applies to a
    task that never set it),
  * the task's own choice wins,
  * the *delivery job* inherits too — not just the visible transform — because
    send-time behaviour (auto-delete, the bot route) is decided from the rule
    object the pipeline hands to the queue,
  * the resolved values are a copy: the loaded rule is untouched, the database
    is unchanged, and saving the copy is refused rather than quietly turning a
    bot's defaults into the task's choices,
  * a token that no longer exists degrades to "no defaults" instead of stopping
    the forward.
"""
from __future__ import annotations

import pytest

from core.application.effective_settings import INHERITED_MARK, materialize
from core.application.pipeline import DurableMessagePipeline
from core.application.repositories import BotToken
from core.application.use_cases import MessageForwardingUseCase
from core.domain.entities import ForwardRule, MessagePayload
from core.domain.rule_settings import apply_settings, read_settings
from core.domain.value_objects import ForwardMode, RoutingType
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteAIConfigRepository,
    SqliteBotTokenRepository,
    SqliteDeliveryQueueRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.durable_queue import DurableQueueManager

OWNER = 5094837833
EMOJI_TEXT = "hello \U0001F600 world"


class FakeQueueManager:
    """Records what the pipeline asked to deliver, and with which rule."""

    def __init__(self):
        self.calls = []

    async def enqueue_delivery(self, rule, payload, target, text, evaluation=None, **kw):
        self.calls.append({"rule": rule, "target": target, "text": text})
        return True

    def get_stats(self):
        return {}


class Env:
    def __init__(self):
        self.db = SqliteDatabase(":memory:")
        self.crypto = CryptoService("test-master-key")
        self.rules = SqliteForwardRuleRepository(self.db)
        self.filters = SqliteFilterRuleRepository(self.db)
        self.ai = SqliteAIConfigRepository(self.db, self.crypto)
        self.tokens = SqliteBotTokenRepository(self.db, self.crypto)
        self.queue = SqliteDeliveryQueueRepository(self.db)
        self.maps = SqliteMessageMapRepository(self.db)
        self.processed = SqliteProcessedMessageRepository(self.db)
        self.db.execute(
            "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
            " username, first_name, is_active, is_authorized, created_at, updated_at)"
            " VALUES ('s1', '+1000', '', '', '', '', 1, 1, 0, 0)", (),
        )
        self.session_id = "s1"
        self._rules_made = 0

    def _next_rule_id(self) -> str:
        self._rules_made += 1
        return "r-1" if self._rules_made == 1 else f"r-{self._rules_made}"

    async def add_bot(self, token_id: str = "tok-1", defaults=None, owner: int = OWNER):
        from time import time as _now
        await self.tokens.add(BotToken(
            id=token_id, label=f"@{token_id}", token="7878773795:AAsecret",
            owner_user_id=owner, created_at=int(_now()), updated_at=int(_now()),
            defaults=defaults or {},
        ))
        return token_id

    async def add_rule(self, *, token_id: str = "", source: str = "-1001",
                       rule_id: str = "", owner: int = OWNER, **settings):
        rule = ForwardRule(
            id=rule_id or self._next_rule_id(), name="a task", session_id=self.session_id,
            source_chat_id=source, target_chat_id="-1002",
            routing_type=RoutingType.CHANNEL_TO_CHANNEL,
            forward_mode=ForwardMode.COPY_MESSAGE, owner_user_id=owner,
        )
        if token_id:
            rule.bot_token_id = token_id
        await self.rules.add(rule)
        if settings:
            loaded = await self.rules.get_by_id(rule.id)
            apply_settings(loaded, settings)
            await self.rules.update(loaded)   # the rule itself, not a resolved copy
        return await self.rules.get_by_id(rule.id)

    def use_case(self) -> MessageForwardingUseCase:
        return MessageForwardingUseCase(
            self.rules, self.filters, self.ai, None,
            processed_repo=self.processed, bot_tokens=self.tokens,
        )

    def durable(self) -> DurableMessagePipeline:
        return DurableMessagePipeline(
            rule_repo=self.rules, filter_repo=self.filters,
            queue_manager=DurableQueueManager(
                self.queue, self.maps, None, num_workers=1, rule_repo=self.rules,
            ),
            msg_map_repo=self.maps, processed_repo=self.processed,
            bot_tokens=self.tokens,
        )


@pytest.fixture()
def env():
    return Env()


# --------------------------------------------------------------------------- #
# The transform runs with the bot's value
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_task_without_its_own_setting_uses_the_bots_default(env):
    await env.add_bot(defaults={"remove_emojis": True})
    await env.add_rule(token_id="tok-1")
    uc = env.use_case()
    sent = {}

    async def sender(payload, rule, text, evaluation):
        sent["text"] = text
        return True

    uc.sender = sender
    out = await uc.process_message(MessagePayload(message_id=1, chat_id="-1001", text=EMOJI_TEXT))

    assert out.forwarded
    assert "\U0001F600" not in sent["text"], "the bot's default should have applied"


@pytest.mark.asyncio
async def test_the_tasks_own_choice_beats_the_bots_default(env):
    await env.add_bot(defaults={"remove_emojis": True})
    rule = await env.add_rule(token_id="tok-1")
    # The user saw the bot's "on" and turned it off for this task: an explicit
    # choice that happens to equal the registry default.
    apply_settings(rule, {"remove_emojis": False}, effective_before={"remove_emojis": True})
    await env.rules.update(rule)

    uc = env.use_case()
    sent = {}

    async def sender(payload, rule, text, evaluation):
        sent["text"] = text
        return True

    uc.sender = sender
    await uc.process_message(MessagePayload(message_id=1, chat_id="-1001", text=EMOJI_TEXT))
    assert "\U0001F600" in sent["text"], "the task's own choice must win"


@pytest.mark.asyncio
async def test_a_task_with_no_bot_attached_is_untouched(env):
    rule = await env.add_rule()
    assert await materialize(rule, env.tokens) is rule


@pytest.mark.asyncio
async def test_a_token_that_no_longer_exists_degrades_to_no_defaults(env):
    await env.add_rule(token_id="tok-deleted")
    uc = env.use_case()
    sent = {}

    async def sender(payload, rule, text, evaluation):
        sent["text"] = text
        return True

    uc.sender = sender
    out = await uc.process_message(MessagePayload(message_id=1, chat_id="-1001", text=EMOJI_TEXT))
    assert out.forwarded and sent["text"] == EMOJI_TEXT


# --------------------------------------------------------------------------- #
# Delivery inherits as well as the transform
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_enqueued_job_carries_the_inherited_settings(env):
    """Send-time behaviour is decided from the rule the pipeline enqueues with.

    Auto-delete is read from the job, not from the task at delivery time, so it
    inherits only if the resolved rule is what got enqueued. The add-on gate is
    consulted at the same moment, so the entitlement has to inherit with it —
    that is the second inherited setting here, and it is what makes the 42 real
    rather than a number nobody would act on.
    """
    await env.add_bot(defaults={
        "auto_delete_seconds": 42,
        "paid_addons": {"auto_delete_message": True},
        "remove_emojis": True,
    })
    await env.add_rule(token_id="tok-1")
    pipeline = env.durable()

    ctx = await pipeline.process({"chat_id": "-1001", "message_id": 7, "text": EMOJI_TEXT})
    assert ctx.target_jobs, "the message should have been enqueued"

    job = await env.queue.claim_batch("w-1", 1, lease_duration=30)
    assert job, "a delivery job should be waiting"
    assert job[0].payload_data.get("auto_delete_seconds") == 42
    assert job[0].rule_id == "r-1"


# --------------------------------------------------------------------------- #
# Resolving must not rewrite the task
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolving_returns_a_copy_and_leaves_the_stored_rule_alone(env):
    await env.add_bot(defaults={"remove_emojis": True})
    rule = await env.add_rule(token_id="tok-1")

    resolved = await materialize(rule, env.tokens)

    assert resolved is not rule
    assert resolved.remove_emojis is True
    assert rule.remove_emojis is False, "the loaded entity must not be modified"
    assert getattr(resolved, INHERITED_MARK) == ["remove_emojis"]
    # ...and the database never learned about it.
    stored = await env.rules.get_by_id("r-1")
    assert stored.remove_emojis is False
    assert not (stored.metadata or {}).get("remove_emojis")


@pytest.mark.asyncio
async def test_saving_a_resolved_copy_is_refused(env):
    """The guard that turns a silent misconfiguration into a loud failure.

    If a resolved rule were ever saved, the bot's defaults would become the
    task's own values and it would stop tracking the bot.
    """
    await env.add_bot(defaults={"remove_emojis": True})
    rule = await env.add_rule(token_id="tok-1")
    resolved = await materialize(rule, env.tokens)

    with pytest.raises(ValueError) as err:
        await env.rules.update(resolved)
    assert "inherited bot defaults" in str(err.value)
    assert "remove_emojis" in str(err.value)
    # The stored row is intact.
    assert (await env.rules.get_by_id("r-1")).remove_emojis is False


@pytest.mark.asyncio
async def test_a_change_to_a_bot_default_reaches_the_next_message(env):
    """No caching: a user who edits their bot's defaults sees it immediately."""
    await env.add_bot(defaults={"remove_emojis": True})
    await env.add_rule(token_id="tok-1")
    uc = env.use_case()
    seen = []

    async def sender(payload, rule, text, evaluation):
        seen.append(text)
        return True

    uc.sender = sender
    await uc.process_message(MessagePayload(message_id=1, chat_id="-1001", text=EMOJI_TEXT))
    await env.tokens.set_defaults("tok-1", OWNER, {})          # the owner turns it off
    await uc.process_message(MessagePayload(message_id=2, chat_id="-1001", text=EMOJI_TEXT))

    assert "\U0001F600" not in seen[0]
    assert "\U0001F600" in seen[1]


@pytest.mark.asyncio
async def test_two_tasks_on_different_bots_do_not_share_defaults(env):
    await env.add_bot("tok-a", defaults={"remove_emojis": True})
    await env.add_bot("tok-b", defaults={})
    first = await env.add_rule(token_id="tok-a", rule_id="r-a")
    second = await env.add_rule(token_id="tok-b", rule_id="r-b")

    resolved_first = await materialize(first, env.tokens)
    resolved_second = await materialize(second, env.tokens)

    assert resolved_first.remove_emojis is True
    assert resolved_second is second and resolved_second.remove_emojis is False


# --------------------------------------------------------------------------- #
# Ownership: a task cannot inherit a bot it does not own
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_task_cannot_inherit_a_bot_another_tenant_owns(env):
    """A transplanted token id must not hand the other tenant's settings over.

    The token id lives in the rule's metadata, so it is a client-visible routing
    identifier. Reading its defaults without the owner would copy another
    tenant's configuration into this task (and, in the worker, deliver through
    their credential).
    """
    other = 6278059256
    await env.add_bot("tok-mine", defaults={"remove_emojis": True}, owner=OWNER)
    await env.add_bot("tok-theirs", defaults={"remove_emojis": True}, owner=other)

    mine = await env.add_rule(token_id="tok-mine")
    transplanted = await env.add_rule(token_id="tok-theirs")

    resolved_mine = await materialize(
        await env.rules.get_by_id(mine.id), env.tokens
    )
    resolved_transplanted = await materialize(
        await env.rules.get_by_id(transplanted.id), env.tokens
    )

    assert getattr(resolved_mine, INHERITED_MARK, None), "the task's own bot must still be inherited"
    assert not getattr(resolved_transplanted, INHERITED_MARK, None), (
        "a task must not inherit the defaults of a bot owned by somebody else"
    )
    assert read_settings(resolved_transplanted).get("remove_emojis") in (False, None), (
        "another tenant's defaults leaked into this task"
    )
    # The row exists and is readable — the refusal is about ownership, not about
    # the token having disappeared.
    assert (await env.tokens.get_by_id("tok-theirs")) is not None


@pytest.mark.asyncio
async def test_a_foreign_bot_never_reaches_the_delivered_message(env):
    """The same boundary, measured where it matters: the text that goes out."""
    other = 6278059256
    await env.add_bot("tok-theirs", defaults={"remove_emojis": True}, owner=other)
    await env.add_rule(token_id="tok-theirs")
    uc = env.use_case()
    sent = {}

    async def sender(payload, rule, text, evaluation):
        sent["text"] = text
        return True

    uc.sender = sender
    await uc.process_message(MessagePayload(message_id=1, chat_id="-1001", text=EMOJI_TEXT))

    assert "\U0001F600" in sent["text"], (
        "another tenant's \"remove emojis\" default was applied to this task's message"
    )
