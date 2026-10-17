"""Delete sync must work for every source chat, not just channels.

Telegram's non-channel delete update (``updateDeleteMessages``) carries message
ids only, and pyrogram's parser turns those into ``Message(chat=None)``. The
dispatcher required a chat id, so a delete in a private chat or basic group was
silently dropped and the task panel's "Delete Sync" switch did nothing — while
the same switch worked for channels. These tests pin both halves of the fix:
the dispatcher must hand unresolved ids on, and the sync engine must resolve
them from the message map without ever deleting the wrong copy.
"""
from types import SimpleNamespace

import pytest

from core.domain.entities import DeliveryStatus, ForwardRule, MessageMapping
from core.infrastructure.telegram.sync_engine import SyncEngine

SOURCE = "7878773795"       # private chat: positive id, no channel context
TARGET = "-1003915313919"


def _mapping(msg_id: int = 930729, chat: str = SOURCE, target_msg: int = 72, rule: str = "r1") -> MessageMapping:
    return MessageMapping(
        id=f"m-{chat}-{msg_id}",
        rule_id=rule,
        source_chat_id=chat,
        source_message_id=msg_id,
        target_chat_id=TARGET,
        target_message_id=target_msg,
        delivery_status=DeliveryStatus.SENT,
    )


class _MapRepo:
    """Minimal stand-in for IMessageMapRepository (only the two lookups used)."""

    def __init__(self, mappings: list[MessageMapping]):
        self.mappings = mappings
        self.by_source_calls: list[tuple] = []
        self.by_id_calls: list[int] = []

    async def get_by_source(self, chat_id, message_id):
        self.by_source_calls.append((str(chat_id), int(message_id)))
        return [m for m in self.mappings if str(m.source_chat_id) == str(chat_id) and m.source_message_id == int(message_id)]

    async def list_by_source_message_id(self, message_id):
        self.by_id_calls.append(int(message_id))
        return [m for m in self.mappings if m.source_message_id == int(message_id)]


class _RuleRepo:
    def __init__(self, sync_deletes: bool = True, active: bool = True):
        self.sync_deletes = sync_deletes
        self.active = active

    async def get_by_id(self, rule_id):
        rule = ForwardRule(id=rule_id, session_id="s1")
        rule.sync_deletes = self.sync_deletes
        rule.is_active = self.active
        return rule


class _Client:
    def __init__(self):
        self.deleted: list[tuple] = []

    async def delete_messages(self, chat_id, message_ids):
        self.deleted.append((chat_id, tuple(message_ids)))


def _engine(mappings, sync_deletes=True, active=True):
    engine = SyncEngine.__new__(SyncEngine)
    engine._map = _MapRepo(mappings)
    engine._rules = _RuleRepo(sync_deletes, active)
    client = _Client()
    engine._pool = SimpleNamespace(get=lambda sid: client)
    return engine, client


# --------------------------------------------------------------------------- #
# Chat-less deletes (private chat / basic group)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_chat_less_delete_removes_the_copy(caplog):
    engine, client = _engine([_mapping()])

    with caplog.at_level("INFO"):
        n = await engine.handle_delete_without_chat([930729])

    assert n == 1
    assert client.deleted == [(int(TARGET), (72,))]
    assert "SYNC_DELETE" in caplog.text, "a synced delete must be visible in the log"
    assert engine._map.by_id_calls == [930729], "the id is resolved through the map, not guessed"


@pytest.mark.asyncio
async def test_an_ambiguous_id_is_never_guessed(caplog):
    # Two monitored chats can each hold message id 7. Deleting the wrong copy
    # is worse than missing a delete, so the update is skipped and logged.
    engine, client = _engine([
        _mapping(msg_id=7, chat="7878773795", target_msg=72),
        _mapping(msg_id=7, chat="-1004400615270", target_msg=73),
    ])

    with caplog.at_level("WARNING"):
        n = await engine.handle_delete_without_chat([7])

    assert n == 0
    assert client.deleted == []
    assert "SYNC_DELETE_AMBIGUOUS" in caplog.text


@pytest.mark.asyncio
async def test_chat_less_delete_ignores_ids_we_never_forwarded():
    engine, client = _engine([_mapping()])
    assert await engine.handle_delete_without_chat([5, 6]) == 0
    assert client.deleted == []


@pytest.mark.asyncio
async def test_chat_less_delete_respects_the_rule_switch():
    engine, client = _engine([_mapping()], sync_deletes=False)
    assert await engine.handle_delete_without_chat([930729]) == 0
    assert client.deleted == [], "\"Delete Sync: Ignore\" must not remove anything"


@pytest.mark.asyncio
async def test_chat_less_delete_ignores_a_paused_rule():
    engine, client = _engine([_mapping()], active=False)
    assert await engine.handle_delete_without_chat([930729]) == 0
    assert client.deleted == []


@pytest.mark.asyncio
async def test_a_missing_client_is_reported_not_swallowed(caplog):
    engine, _ = _engine([_mapping()])
    engine._pool = SimpleNamespace(get=lambda sid: None)

    with caplog.at_level("WARNING"):
        assert await engine.handle_delete_without_chat([930729]) == 0

    assert "SYNC_DELETE_NO_CLIENT" in caplog.text


# --------------------------------------------------------------------------- #
# Channel deletes keep using the chat-scoped lookup
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_channel_delete_still_uses_the_chat_scoped_lookup():
    engine, client = _engine([_mapping(msg_id=930729, chat="-1004400615270", target_msg=71)])

    n = await engine.handle_delete("-1004400615270", [930729])

    assert n == 1
    assert engine._map.by_source_calls == [("-1004400615270", 930729)]
    assert engine._map.by_id_calls == [], "a channel delete needs no id-only fallback"
    assert client.deleted == [(int(TARGET), (71,))]


@pytest.mark.asyncio
async def test_channel_delete_for_an_unmapped_message_is_a_noop():
    engine, client = _engine([_mapping(msg_id=930729, chat="-1004400615270")])
    assert await engine.handle_delete("-1004400615270", [1]) == 0
    assert client.deleted == []


# --------------------------------------------------------------------------- #
# Dispatcher: unresolved ids must reach the engine
# --------------------------------------------------------------------------- #


class _RecordingEngine:
    def __init__(self):
        self.by_chat: list[tuple] = []
        self.orphans: list[list] = []

    async def handle_delete(self, chat_id, msg_ids):
        self.by_chat.append((chat_id, list(msg_ids)))
        return len(msg_ids)

    async def handle_delete_without_chat(self, msg_ids):
        self.orphans.append(list(msg_ids))
        return len(msg_ids)


def _dispatcher(engine):
    from core.infrastructure.telegram.dispatcher import MessageDispatcher

    return MessageDispatcher(
        pool=SimpleNamespace(get=lambda sid: None),
        use_case=SimpleNamespace(),
        sync_engine=engine,
    )


@pytest.mark.asyncio
async def test_dispatcher_routes_channel_deletes_by_chat():
    engine = _RecordingEngine()
    handler = _dispatcher(engine)._make_deleted_handler()

    await handler(None, [SimpleNamespace(id=930722, chat=SimpleNamespace(id=-1004400615270))])

    assert engine.by_chat == [("-1004400615270", [930722])]
    assert engine.orphans == []


@pytest.mark.asyncio
async def test_dispatcher_hands_chat_less_deletes_to_the_id_only_path():
    engine = _RecordingEngine()
    handler = _dispatcher(engine)._make_deleted_handler()

    await handler(None, [SimpleNamespace(id=930729, chat=None)])

    assert engine.orphans == [[930729]], "a delete without a chat must not be dropped"
    assert engine.by_chat == []


@pytest.mark.asyncio
async def test_dispatcher_splits_a_mixed_batch():
    engine = _RecordingEngine()
    handler = _dispatcher(engine)._make_deleted_handler()

    await handler(None, [
        SimpleNamespace(id=1, chat=SimpleNamespace(id=-1001)),
        SimpleNamespace(id=2, chat=None),
        SimpleNamespace(id=3, chat=SimpleNamespace(id=-1001)),
        SimpleNamespace(id=4, chat=None),
    ])

    assert engine.by_chat == [("-1001", [1, 3])]
    assert engine.orphans == [[2, 4]]


@pytest.mark.asyncio
async def test_dispatcher_without_a_sync_engine_does_nothing():
    handler = _dispatcher(None)._make_deleted_handler()
    await handler(None, [SimpleNamespace(id=1, chat=None)])


# --------------------------------------------------------------------------- #
# The new lookup, against the real SQLite repository
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sqlite_lookup_by_id_ignores_the_chat(tmp_path):
    from core.infrastructure.persistence.database import SqliteDatabase
    from core.infrastructure.persistence.sqlite_repositories import SqliteMessageMapRepository

    repo = SqliteMessageMapRepository(SqliteDatabase(str(tmp_path / "del.db")))
    await repo.add_or_update(_mapping(msg_id=930729, chat=SOURCE, target_msg=72))
    await repo.add_or_update(_mapping(msg_id=930729, chat="-1004400615270", target_msg=71))
    await repo.add_or_update(_mapping(msg_id=5, chat=SOURCE, target_msg=9))

    found = await repo.list_by_source_message_id(930729)

    assert sorted(m.target_message_id for m in found) == [71, 72]
    assert {m.source_chat_id for m in found} == {SOURCE, "-1004400615270"}
    assert await repo.list_by_source_message_id(404) == []
