"""Per-task option logic — one authority per switch, and no duplicate posts.

Regression suite for the task panel ("Rule Edit Draft") options whose stored
state and runtime behaviour had drifted apart:

  * AI Rewrite read "Off" while the pipeline rewrote every message, because
    the pipeline gated on ``ai_config_id`` alone and the panel badge was a
    session-cached string.
  * "Message Edit: Sync" re-posted the edited message as a duplicate instead
    of updating the delivered copy: the sync engine required ``sync_edits``
    (which no surface ever wrote) and then the dispatcher fell through to the
    pipeline as well.

Each test fails on the pre-fix code and passes on the fixed one.
"""
from types import SimpleNamespace

import pytest

from core.domain.entities import (
    DeliveryStatus,
    ForwardRule,
    MessageMapping,
    MessagePayload,
)
from core.domain.services import FilterEngine
from core.infrastructure.telegram.sync_engine import SyncEngine


def _payload(**kw) -> MessagePayload:
    base = {"chat_id": "-1001", "message_id": 5, "text": "original"}
    base.update(kw)
    return MessagePayload(**base)


# --------------------------------------------------------------------------- #
# AI Rewrite: one resolver, three writers
# --------------------------------------------------------------------------- #


def test_ai_rewrite_needs_a_config_and_an_open_addon_switch():
    rule = ForwardRule()

    # Nothing configured: off.
    assert rule.ai_rewrite_active is False

    # A config is consent, as it always was — API clients, backups and the AI
    # menu attach a config to switch rewriting on.
    rule.ai_config_id = "cfg-1"
    assert rule.ai_rewrite_active is True

    # The task panel's paid-addon switch is the explicit veto. This was the
    # live bug: the panel read "Off" (paid_addons.ai_rewrite=false) while the
    # pipeline still rewrote every message.
    rule.paid_addons = {"ai_rewrite": False}
    assert rule.ai_rewrite_active is False

    rule.paid_addons = {"ai_rewrite": True}
    assert rule.ai_rewrite_active is True

    # Removing the config is always off, whatever the switch says.
    rule.ai_config_id = None
    assert rule.ai_rewrite_active is False


def test_ai_state_survives_a_draft_round_trip():
    rule = ForwardRule(source_chat_id="-1001", target_chat_id="-1002", session_id="s1")
    rule.ai_config_id = "cfg-1"
    rule.smart_ai_rewrite = True
    rule.paid_addons = {"ai_rewrite": True}

    draft = rule.to_draft()
    assert draft["smart_ai_rewrite"] is True
    assert draft["paid_addons"] == {"ai_rewrite": True}

    restored = ForwardRule()
    restored.apply_draft(draft)
    assert restored.ai_config_id == "cfg-1"
    assert restored.ai_rewrite_active is True


# --------------------------------------------------------------------------- #
# Message Edit: the panel switch decides, one field
# --------------------------------------------------------------------------- #


def test_sync_edits_effective_follows_the_panel_switch():
    rule = ForwardRule()

    rule.ignore_edits = False  # panel shows "Message Edit: Sync"
    assert rule.sync_edits_effective is True

    rule.ignore_edits = True  # panel shows "Message Edit: Ignore"
    assert rule.sync_edits_effective is False

    # A stale legacy key cannot re-enable edits behind the panel's back: the
    # panel's Ignore is a veto, and `SyncEngine.handle_edit` skips such a rule
    # before it ever consults this property (measured on the deployed image:
    # every "Ignore" row left the copy untouched, whatever the legacy key said).
    # Reporting "Sync" here for a task whose panel reads "Ignore" was the
    # disagreement between the two authorities that this property used to have.
    rule.metadata["sync_edits"] = True
    assert rule.sync_edits_effective is False

    # It still decides when the panel allows edits, so clients that only know
    # the old field keep their meaning.
    rule.ignore_edits = False
    rule.metadata["sync_edits"] = False
    assert rule.sync_edits_effective is False
    rule.metadata["sync_edits"] = True
    assert rule.sync_edits_effective is True


@pytest.mark.asyncio
async def test_panel_sync_updates_the_target_copy_in_place():
    mapping = MessageMapping(
        id="m1",
        rule_id="r1",
        source_chat_id="-1001",
        source_message_id=5,
        target_chat_id="-1002",
        target_message_id=77,
        delivery_status=DeliveryStatus.SENT,
    )

    class FakeMap:
        async def get_by_source(self, chat, mid):
            return [mapping] if int(mid) == 5 else []

    class FakeRuleRepo:
        async def get_by_id(self, rid):
            rule = ForwardRule(id="r1", session_id="s1")
            rule.ignore_edits = False  # panel: Sync. No sync_edits key at all.
            return rule

    edited: list[dict] = []
    sent: list[dict] = []

    class FakeClient:
        async def edit_message_text(self, chat_id, message_id, text):
            edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})

        async def send_message(self, chat_id, text):
            sent.append({"chat_id": chat_id, "text": text})

    class FakePool:
        def get(self, sid):
            return FakeClient()

    engine = SyncEngine.__new__(SyncEngine)
    engine._map = FakeMap()
    engine._rules = FakeRuleRepo()
    engine._pool = FakePool()
    engine._filters = None
    engine._filter_engine = FilterEngine()

    n = await engine.handle_edit(_payload(), "edited text")

    assert n == 1
    assert edited == [{"chat_id": -1002, "message_id": 77, "text": "edited text"}]
    assert sent == [], "an edit must never be posted as a new message"


@pytest.mark.asyncio
async def test_panel_ignore_leaves_the_target_copy_alone():
    mapping = MessageMapping(
        id="m1",
        rule_id="r1",
        source_chat_id="-1001",
        source_message_id=5,
        target_chat_id="-1002",
        target_message_id=77,
        delivery_status=DeliveryStatus.SENT,
    )

    class FakeMap:
        async def get_by_source(self, chat, mid):
            return [mapping]

    class FakeRuleRepo:
        async def get_by_id(self, rid):
            rule = ForwardRule(id="r1", session_id="s1")
            rule.ignore_edits = True
            return rule

    class FakeClient:
        touched = 0

        async def edit_message_text(self, **kw):
            FakeClient.touched += 1

    engine = SyncEngine.__new__(SyncEngine)
    engine._map = FakeMap()
    engine._rules = FakeRuleRepo()
    engine._pool = SimpleNamespace(get=lambda sid: FakeClient())
    engine._filters = None

    assert await engine.handle_edit(_payload(), "edited") == 0
    assert FakeClient.touched == 0


# --------------------------------------------------------------------------- #
# Dispatcher routing: a claimed edit is never forwarded again
# --------------------------------------------------------------------------- #


def _dispatcher(engine):
    from core.infrastructure.telegram.dispatcher import MessageDispatcher

    return MessageDispatcher(pool=SimpleNamespace(get=lambda sid: None), use_case=SimpleNamespace(), sync_engine=engine)


class _FakeSyncEngine:
    def __init__(self, mapped: bool):
        self.mapped = mapped
        self.edits: list[tuple] = []

    async def has_mapping(self, chat_id, message_id):
        return self.mapped

    async def handle_edit(self, payload, text):
        self.edits.append((payload.message_id, text))
        return 1


@pytest.mark.asyncio
async def test_edit_of_a_delivered_message_is_claimed_and_never_forwarded():
    engine = _FakeSyncEngine(mapped=True)
    d = _dispatcher(engine)

    claimed = await d._route_edit(_payload())

    assert claimed is True
    assert engine.edits == [(5, "original")]


@pytest.mark.asyncio
async def test_edit_of_an_undelivered_message_is_forwarded_once():
    engine = _FakeSyncEngine(mapped=False)
    d = _dispatcher(engine)

    claimed = await d._route_edit(_payload())

    assert claimed is False
    assert engine.edits == [], "no mapping means the pipeline owns the first delivery"


@pytest.mark.asyncio
async def test_edit_without_a_sync_engine_is_not_claimed():
    d = _dispatcher(None)
    assert await d._route_edit(_payload()) is False


# --------------------------------------------------------------------------- #
# Multi-photo: the three modes reach the worker
# --------------------------------------------------------------------------- #


def _album_job(album_mode: str):
    from core.domain.entities import DeliveryJob

    return DeliveryJob(
        id="job-1",
        rule_id="r1",
        source_chat_id="-1001",
        source_message_id=5,
        target_chat_id="-1002",
        payload_data={
            "source_chat_id": "-1001",
            "target_chat_id": "-1002",
            "text": "album caption",
            "forward_mode": "COPY_MESSAGE",
            "content_mode": "AUTO",
            "has_media": True,
            "media_type": "photo",
            "media_group_id": "grp-1",
            "album_message_ids": [5, 6, 7],
            "album_mode": album_mode,
        },
    )


class _AlbumClient:
    def __init__(self):
        self.calls: list[tuple] = []

    async def copy_media_group(self, chat_id, from_chat_id, message_id, **kw):
        self.calls.append(("group", message_id))
        return [SimpleNamespace(id=100)]

    async def copy_message(self, chat_id, from_chat_id, message_id, caption=None, **kw):
        self.calls.append(("copy", message_id))
        return SimpleNamespace(id=message_id)


async def _dispatch(album_mode: str):
    from core.infrastructure.telegram.durable_queue import DurableQueueManager

    manager = DurableQueueManager.__new__(DurableQueueManager)
    client = _AlbumClient()
    await manager._dispatch_to_telegram(client, _album_job(album_mode))
    return client.calls


@pytest.mark.asyncio
async def test_album_mode_full_album_keeps_the_media_group():
    assert await _dispatch("album") == [("group", 5)]


@pytest.mark.asyncio
async def test_album_mode_first_sends_only_the_first_item():
    assert await _dispatch("first") == [("copy", 5)]


@pytest.mark.asyncio
async def test_album_mode_split_sends_every_item_in_order():
    assert await _dispatch("split") == [("copy", 5), ("copy", 6), ("copy", 7)]

