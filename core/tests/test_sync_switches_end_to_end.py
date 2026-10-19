"""Per-task feature switches must decide real engine behaviour, not just a DB value.

STEER 01 Edit / STEER 02 Delete / STEER 03 Reply.

Every test drives the *real* engine (`SyncEngine`) through the *real* entry
point (the dispatcher handler, or the engine's own public method) with the
repositories the engine reloads its rule from. No test mutates a rule object it
also hands to the engine: the engine re-reads the rule from the repository on
every event, so a test that mutates its own copy asserts on a different object
than production uses — that is precisely how the Edit switch stayed inert in
production while every unit test passed.
"""

from __future__ import annotations

import asyncio

import pytest

from core.domain.entities import (
    DeliveryStatus,
    ForwardRule,
    MessageMapping,
    MessagePayload,
)
from core.infrastructure.telegram import dispatcher as dispatcher_module
from core.infrastructure.telegram.dispatcher import MessageDispatcher
from core.infrastructure.telegram.sync_engine import SyncEngine

OWNER = 5094837833
RULE_ID = "rule-sync-1"
SESSION = "sess-a"
SOURCE = "-1004400615270"
TARGET = "-1003915313919"


# --------------------------------------------------------------------------- #
# Fakes — repositories the engine reloads from
# --------------------------------------------------------------------------- #
class StubMapRepo:
    def __init__(self, mappings=()):
        self.rows = list(mappings)
        self.lookup_error: Exception | None = None
        self.upserts: list[MessageMapping] = []

    async def get_by_source(self, chat_id, message_id):
        if self.lookup_error is not None:
            raise self.lookup_error
        return [
            m for m in self.rows
            if str(m.source_chat_id) == str(chat_id)
            and int(m.source_message_id) == int(message_id)
        ]

    async def add_or_update(self, mapping):
        self.upserts.append(mapping)
        for i, m in enumerate(self.rows):
            if (
                str(m.rule_id) == str(mapping.rule_id)
                and str(m.source_chat_id) == str(mapping.source_chat_id)
                and int(m.source_message_id) == int(mapping.source_message_id)
                and str(m.target_chat_id) == str(mapping.target_chat_id)
            ):
                self.rows[i] = mapping
                return mapping
        self.rows.append(mapping)
        return mapping


class StubRuleRepo:
    def __init__(self, rules):
        self.rules = {r.id: r for r in rules}

    async def get_by_id(self, rule_id):
        return self.rules.get(str(rule_id))


class StubClient:
    def __init__(self, fail_edits=0, wait_seconds=0):
        self.edits: list[dict] = []
        self.captions: list[dict] = []
        self.deletes: list[dict] = []
        self.sends: list[dict] = []
        self.fail_edits = fail_edits
        self.wait_seconds = wait_seconds

    async def edit_message_text(self, **kw):
        if self.fail_edits > 0:
            self.fail_edits -= 1
            raise FloodWait(self.wait_seconds or 3)
        self.edits.append(kw)
        return True

    async def edit_message_caption(self, **kw):
        self.captions.append(kw)
        return True

    async def delete_messages(self, **kw):
        self.deletes.append(kw)
        return True

    async def send_message(self, **kw):
        self.sends.append(kw)
        return SimpleSent(len(self.sends))


SimpleSent = type("SimpleSent", (), {"__init__": lambda self, mid: setattr(self, "id", mid)})


class FloodWait(Exception):
    """Shape of pyrofork's RPC error: carries the wait in ``value`` and in the text."""

    def __init__(self, seconds):
        super().__init__(f"FLOOD_WAIT_{seconds}")
        self.value = seconds


class StubPool:
    def __init__(self, client):
        self._client = client

    def get(self, session_id):
        return self._client


class StubPipeline:
    """Records that the event fell through to normal forwarding."""

    def __init__(self):
        self.calls: list[MessagePayload] = []

    async def process(self, payload, *args, **kwargs):
        self.calls.append(payload)
        return True


class StubUseCase:
    """The dispatcher assigns its own sender onto the use case."""

    sender = None

    def __init__(self):
        self.enqueued: list[MessagePayload] = []

    async def execute(self, payload, *args, **kwargs):
        self.enqueued.append(payload)
        return []


# --------------------------------------------------------------------------- #
# Fixtures / factories
# --------------------------------------------------------------------------- #
def make_rule(**metadata) -> ForwardRule:
    return ForwardRule(
        id=RULE_ID,
        session_id=SESSION,
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        owner_user_id=OWNER,
        metadata=dict(metadata),
    )


def mapping(source_id, target_id=555, rule_id=RULE_ID, status=DeliveryStatus.SENT) -> MessageMapping:
    return MessageMapping(
        rule_id=rule_id,
        source_chat_id=SOURCE,
        source_message_id=source_id,
        target_chat_id=TARGET,
        target_message_id=target_id,
        delivery_status=status,
    )


def payload_for(message_id=77, reply_to=None, text="hello there", **kw) -> MessagePayload:
    return MessagePayload(
        message_id=message_id,
        chat_id=SOURCE,
        chat_type="channel",
        text=text,
        reply_to_message_id=reply_to,
        **kw,
    )


def build_engine(rule, mappings=(), client=None):
    client = client or StubClient()
    maps = StubMapRepo(mappings)
    engine = SyncEngine(maps, StubRuleRepo([rule]), StubPool(client))
    engine._sleep = _no_sleep  # retries are asserted, never waited
    return engine, maps, client


async def _no_sleep(seconds):  # pragma: no cover - trivial
    return None


# --------------------------------------------------------------------------- #
# STEER 01 — Edit
# --------------------------------------------------------------------------- #
class TestEditSwitchDecidesRuntimeBehaviour:
    """The panel's Ignore must veto a stale legacy ``sync_edits`` key."""

    def test_panel_ignore_vetoes_a_stale_legacy_key(self):
        rule = make_rule(sync_edits=True)
        rule.ignore_edits = True
        assert rule.sync_edits_effective is False

    def test_panel_ignore_alone_is_off(self):
        rule = make_rule()
        rule.ignore_edits = True
        assert rule.sync_edits_effective is False

    def test_panel_sync_with_no_legacy_key_is_on(self):
        rule = make_rule()
        rule.ignore_edits = False
        assert rule.sync_edits_effective is True

    def test_legacy_key_still_decides_when_panel_allows_edits(self):
        off = make_rule(sync_edits=False)
        off.ignore_edits = False
        assert off.sync_edits_effective is False
        on = make_rule(sync_edits=True)
        on.ignore_edits = False
        assert on.sync_edits_effective is True

    def test_engine_updates_the_copy_when_the_panel_allows_edits(self):
        async def run():
            rule = make_rule()
            rule.ignore_edits = False
            engine, _, client = build_engine(rule, [mapping(63)])
            edited = await engine.handle_edit(payload_for(message_id=63, text="edited body"), "edited body")
            assert edited == 1
            assert client.edits and client.edits[0]["message_id"] == 555

        asyncio.run(run())

    def test_engine_leaves_the_copy_alone_when_the_panel_says_ignore(self):
        """Regression: the switch was inert while a legacy key sat in metadata."""

        async def run():
            rule = make_rule(sync_edits=True)  # stale value from an older API write
            rule.ignore_edits = True           # what the panel shows
            engine, _, client = build_engine(rule, [mapping(63)])
            edited = await engine.handle_edit(payload_for(message_id=63, text="edited body"), "edited body")
            assert edited == 0
            assert client.edits == []

        asyncio.run(run())

    def test_a_message_never_delivered_is_not_claimed_by_the_edit_path(self):
        async def run():
            engine, _, _ = build_engine(make_rule(), [])
            assert await engine.has_mapping(SOURCE, 999) is False

        asyncio.run(run())

    def test_a_failed_mapping_lookup_claims_the_edit_rather_than_reposting_it(self, caplog):
        """Fail closed: a broken lookup must never let the edit become a new post."""

        async def run():
            engine, maps, _ = build_engine(make_rule(), [])
            maps.lookup_error = RuntimeError("database is locked")
            with caplog.at_level("WARNING"):
                claimed = await engine.has_mapping(SOURCE, 63)
            assert claimed is True

        asyncio.run(run())
        assert "SYNC_PROBE_FAILED" in caplog.text

    def test_a_caption_edit_updates_the_caption(self):
        async def run():
            rule = make_rule()
            engine, _, client = build_engine(rule, [mapping(63)])
            await engine.handle_edit(
                payload_for(message_id=63, text="", caption="new caption", has_media=True),
                "new caption",
            )
            assert client.captions and client.captions[0]["caption"] == "new caption"

        asyncio.run(run())

    def test_an_edit_retries_once_inside_the_flood_wait_cap(self):
        async def run():
            rule = make_rule()
            engine, _, client = build_engine(rule, [mapping(63)], client=StubClient(fail_edits=1, wait_seconds=3))
            edits = await engine.handle_edit(payload_for(message_id=63, text="body"), "body")
            assert edits == 1
            assert client.edits, "the retry must actually reach Telegram"

        asyncio.run(run())

    def test_an_edit_reports_a_flood_wait_beyond_the_cap_instead_of_blocking(self, caplog):
        async def run():
            rule = make_rule()
            engine, _, client = build_engine(rule, [mapping(63)], client=StubClient(fail_edits=5, wait_seconds=600))
            with caplog.at_level("WARNING"):
                edits = await engine.handle_edit(payload_for(message_id=63, text="body"), "body")
            assert edits == 0
            assert client.edits == []

        asyncio.run(run())

    def test_pending_mappings_are_not_updated_as_if_delivered(self):
        async def run():
            rule = make_rule()
            engine, _, client = build_engine(rule, [mapping(63, status=DeliveryStatus.PENDING)])
            assert await engine.handle_edit(payload_for(message_id=63, text="body"), "body") == 0
            assert client.edits == []

        asyncio.run(run())


# --------------------------------------------------------------------------- #
# STEER 02 — Delete
# --------------------------------------------------------------------------- #
class TestDeleteSwitchDecidesRuntimeBehaviour:
    def test_delete_propagates_when_enabled(self):
        async def run():
            rule = make_rule(sync_deletes=True)
            engine, _, client = build_engine(rule, [mapping(63)])
            assert await engine.handle_delete(SOURCE, [63]) == 1
            assert client.deletes and client.deletes[0]["message_ids"] == [555]

        asyncio.run(run())

    def test_delete_is_inert_when_disabled(self):
        async def run():
            rule = make_rule(sync_deletes=False)
            engine, _, client = build_engine(rule, [mapping(63)])
            assert await engine.handle_delete(SOURCE, [63]) == 0
            assert client.deletes == []

        asyncio.run(run())

    def test_delete_of_an_unmapped_message_touches_nothing(self):
        async def run():
            rule = make_rule(sync_deletes=True)
            engine, _, client = build_engine(rule, [])
            assert await engine.handle_delete(SOURCE, [4242]) == 0
            assert client.deletes == []

        asyncio.run(run())

    def test_delete_never_crosses_to_another_rules_target(self):
        async def run():
            rule = make_rule(sync_deletes=True)
            foreign = MessageMapping(
                rule_id="rule-other",
                source_chat_id=SOURCE,
                source_message_id=63,
                target_chat_id="-1009998887776",
                target_message_id=999,
                delivery_status=DeliveryStatus.SENT,
            )
            engine, _, client = build_engine(rule, [mapping(63), foreign])
            removed = await engine.handle_delete(SOURCE, [63])
            assert removed == 1
            assert [c["chat_id"] for c in client.deletes] == [int(TARGET)]

        asyncio.run(run())

    def test_delete_retries_once_inside_the_flood_wait_cap(self):
        async def run():
            rule = make_rule(sync_deletes=True)
            engine, _, client = build_engine(rule, [mapping(63)])
            calls = {"n": 0}
            real = client.delete_messages

            async def flaky(**kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise FloodWait(2)
                return await real(**kw)

            client.delete_messages = flaky
            assert await engine.handle_delete(SOURCE, [63]) == 1
            assert calls["n"] == 2

        asyncio.run(run())


# --------------------------------------------------------------------------- #
# STEER 03 — Reply (the defect was the missing caller, so this tests the wiring)
# --------------------------------------------------------------------------- #
def build_dispatcher(engine):
    pipeline = StubPipeline()
    dispatcher = MessageDispatcher(
        pool=StubPool(engine._pool.get(SESSION)),
        use_case=StubUseCase(),
        sync_engine=engine,
        durable_pipeline=pipeline,
    )
    return dispatcher, pipeline


class FakeMessage:
    """Enough of a pyrogram message for the handler's pre-pipeline stages."""

    chat = None
    id = 1


def test_the_dispatcher_claims_a_reply_and_threads_it(monkeypatch):
    """Before this wiring existed, ``handle_reply`` had no production caller."""

    async def run():
        rule = make_rule(sync_replies=True)
        engine, maps, client = build_engine(rule, [mapping(63, target_id=555)])
        payload = payload_for(message_id=90, reply_to=63, text="my reply")
        monkeypatch.setattr(dispatcher_module, "payload_from_pyrogram", lambda message: payload)

        dispatcher, pipeline = build_dispatcher(engine)
        await dispatcher._make_handler("NEW_MESSAGE")(client, FakeMessage())

        assert len(client.sends) == 1, "the reply must be posted in the target"
        assert client.sends[0]["chat_id"] == int(TARGET)
        assert client.sends[0]["reply_to_message_id"] == 555, "it must stay attached to the mapped copy"
        assert pipeline.calls == [], "a delivered reply must not also be forwarded as a plain message"
        # The reply is now a delivered copy of its own: editable and delete-syncable.
        assert [m.source_message_id for m in maps.rows] == [63, 90]

    asyncio.run(run())


def test_a_redispatched_reply_is_not_posted_twice(monkeypatch):
    async def run():
        rule = make_rule(sync_replies=True)
        engine, _, client = build_engine(rule, [mapping(63, target_id=555)])
        payload = payload_for(message_id=90, reply_to=63, text="my reply")
        monkeypatch.setattr(dispatcher_module, "payload_from_pyrogram", lambda message: payload)

        dispatcher, pipeline = build_dispatcher(engine)
        handler = dispatcher._make_handler("NEW_MESSAGE")
        await handler(client, FakeMessage())
        await handler(client, FakeMessage())  # Telegram redelivery

        assert len(client.sends) == 1
        assert pipeline.calls == [], "a redelivery must not fall through and duplicate the reply"

    asyncio.run(run())


def test_a_reply_falls_through_when_the_switch_is_off(monkeypatch):
    async def run():
        rule = make_rule(sync_replies=False)
        engine, _, client = build_engine(rule, [mapping(63, target_id=555)])
        payload = payload_for(message_id=90, reply_to=63, text="my reply")
        monkeypatch.setattr(dispatcher_module, "payload_from_pyrogram", lambda message: payload)

        dispatcher, pipeline = build_dispatcher(engine)
        await dispatcher._make_handler("NEW_MESSAGE")(client, FakeMessage())

        assert client.sends == []
        assert len(pipeline.calls) == 1, "with the switch off the reply is forwarded as before"

    asyncio.run(run())


def test_a_reply_to_an_unmapped_message_falls_through(monkeypatch):
    async def run():
        rule = make_rule(sync_replies=True)
        engine, _, client = build_engine(rule, [])
        payload = payload_for(message_id=90, reply_to=63, text="my reply")
        monkeypatch.setattr(dispatcher_module, "payload_from_pyrogram", lambda message: payload)

        dispatcher, pipeline = build_dispatcher(engine)
        await dispatcher._make_handler("NEW_MESSAGE")(client, FakeMessage())

        assert client.sends == []
        assert len(pipeline.calls) == 1

    asyncio.run(run())


def test_an_empty_reply_falls_through(monkeypatch):
    async def run():
        rule = make_rule(sync_replies=True)
        engine, _, client = build_engine(rule, [mapping(63, target_id=555)])
        payload = payload_for(message_id=90, reply_to=63, text="   ")
        monkeypatch.setattr(dispatcher_module, "payload_from_pyrogram", lambda message: payload)

        dispatcher, pipeline = build_dispatcher(engine)
        await dispatcher._make_handler("NEW_MESSAGE")(client, FakeMessage())

        assert client.sends == []
        assert len(pipeline.calls) == 1

    asyncio.run(run())


def test_a_failing_reply_send_never_blocks_the_forward(monkeypatch):
    async def run():
        rule = make_rule(sync_replies=True)
        engine, _, client = build_engine(rule, [mapping(63, target_id=555)])
        payload = payload_for(message_id=90, reply_to=63, text="my reply")
        monkeypatch.setattr(dispatcher_module, "payload_from_pyrogram", lambda message: payload)

        async def boom(**kw):
            raise RuntimeError("chat write forbidden")

        client.send_message = boom
        dispatcher, pipeline = build_dispatcher(engine)
        await dispatcher._make_handler("NEW_MESSAGE")(client, FakeMessage())

        assert len(pipeline.calls) == 1, "an undelivered reply must still be forwarded"

    asyncio.run(run())


def test_the_reply_path_does_not_touch_another_tasks_mapping(monkeypatch):
    async def run():
        rule = make_rule(sync_replies=True)
        foreign = MessageMapping(
            rule_id="rule-other",
            source_chat_id=SOURCE,
            source_message_id=63,
            target_chat_id="-1009998887776",
            target_message_id=999,
            delivery_status=DeliveryStatus.SENT,
        )
        engine, _, client = build_engine(rule, [mapping(63, target_id=555), foreign])
        payload = payload_for(message_id=90, reply_to=63, text="my reply")
        monkeypatch.setattr(dispatcher_module, "payload_from_pyrogram", lambda message: payload)

        dispatcher, _ = build_dispatcher(engine)
        await dispatcher._make_handler("NEW_MESSAGE")(client, FakeMessage())

        assert [c["chat_id"] for c in client.sends] == [int(TARGET)]

    asyncio.run(run())


def test_flood_wait_seconds_reads_both_shapes():
    assert SyncEngine.flood_wait_seconds(FloodWait(42)) == 42.0
    assert SyncEngine.flood_wait_seconds(RuntimeError("FLOOD_WAIT_7")) == 7.0
    assert SyncEngine.flood_wait_seconds(RuntimeError("MESSAGE_NOT_MODIFIED")) is None


def test_no_handler_claims_a_reply_without_a_sync_engine(monkeypatch):
    async def run():
        payload = payload_for(message_id=90, reply_to=63, text="my reply")
        monkeypatch.setattr(dispatcher_module, "payload_from_pyrogram", lambda message: payload)
        dispatcher = MessageDispatcher(pool=StubPool(StubClient()), use_case=StubUseCase(),
                                       durable_pipeline=StubPipeline())
        assert await dispatcher._route_reply(payload) is False

    asyncio.run(run())
