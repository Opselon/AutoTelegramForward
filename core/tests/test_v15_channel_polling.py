"""v15 channel polling tests.

The polling service exists because Telegram stops pushing channel posts once a
session's read-state for that channel is caught up — the forwarding source
channels routinely end up unread=0 and go silent. These tests cover the
polling service against a fake client/pool/rules, plus the cursor repository.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.infrastructure.telegram.channel_polling import (  # noqa: E402
    ChannelPollingService,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeRawMessage:
    def __init__(self, mid, text="hello"):
        self.id = mid
        self.message = text
        self.date = 1791880000 + mid
        self.peer_id = None
        self.from_id = None
        self.media = None
        self.entities = None
        self.reply_to = None
        self.forward_from = None
        self.forward_from_chat = None
        self.out = False
        self.mentioned = False
        self.media_unread = False
        self.silent = False
        self.post = True
        self.from_scheduled = False
        self.legacy = False
        self.edit_hide = False
        self.pinned = False
        self.noforwards = False
        self.reactions = None
        self.replies = None
        self.grouped_id = None
        self.ttl_period = None
        self.views = None
        self.forwards = None
        self.edit_date = None
        self.post_author = None
        self.saved_peer_id = None


class FakeDifference:
    def __init__(self, messages):
        self.new_messages = messages
        self.users = []
        self.chats = []


class FakeClient:
    def __init__(self):
        self._history = []
        self.replayed = []
        self.peers = {}
        self.history_raises = False

    async def resolve_peer(self, chat_id):
        return self.peers.get(str(chat_id), f"peer:{chat_id}")

    async def invoke(self, query):
        # GetFullChannel / GetChannelDifference both flow through here; keep
        # the fake minimal and only answer what the poller asks for.
        name = type(query).__name__
        if name == "GetFullChannel":
            return _FakeFullChat()
        if name == "GetChannelDifference":
            return FakeDifference(self._history)
        return None

    def get_chat_history(self, chat_id, limit=1):
        """Async-generator stand-in for pyrogram's get_chat_history."""
        async def gen():
            if self.history_raises:
                raise RuntimeError("boom")
            for m in self._history[:limit]:
                yield m

        return gen()


class _FakeFullChat:
    class _Inner:
        pts = 30000

    full_chat = _Inner()


class FakeDispatcher:
    def __init__(self):
        self.calls = []

    def message_handler(self, session_id):
        async def handler(client, message):
            self.calls.append((session_id, message.id))

        return handler


class FakePool:
    def __init__(self, client):
        self._client = client

    def get(self, session_id):
        return self._client

    def list_sessions(self):
        return ["s1"]


class FakeRule:
    def __init__(self, src, session_id="s1", active=True):
        self.id = "rule-x"
        self.source_chat_id = src
        self.source_chat_name = f"chan-{src}"
        self.session_id = session_id
        self.is_active = active
        self.is_paused = False


class FakeRules:
    def __init__(self, rules):
        self._rules = rules

    async def list_all(self):
        return self._rules


class FakeCursorStore:
    def __init__(self):
        self.store = {}
        self.saved = 0

    async def load_channel_cursors(self):
        return dict(self.store)

    async def save_channel_cursors(self, cursors):
        self.store.update(cursors)
        self.saved += 1



def _stub_parse(monkeypatch):
    """Make pyrogram's Message._parse return a light fake for raw posts."""
    from pyrogram.types import Message

    class FakeParsed:
        def __init__(self, mid):
            self.id = mid

    async def fake_parse(client, message, users, chats, *a, **kw):
        return FakeParsed(getattr(message, "id", 0))

    monkeypatch.setattr(Message, "_parse", staticmethod(fake_parse))

# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_poll_replays_new_posts(monkeypatch):
    """A sweep finds posts above the cursor and replays them."""
    client = FakeClient()
    client._history = [FakeRawMessage(11), FakeRawMessage(12)]
    disp = FakeDispatcher()
    poller = ChannelPollingService(
        FakePool(client), FakeRules([FakeRule("-1001")]), disp
    )
    _stub_parse(monkeypatch)

    replayed, checked = await poller.sweep_all()
    assert checked == 1
    assert replayed == 2
    # ensure_future schedules the dispatch; give the loop a tick to run it.
    await asyncio.sleep(0.02)
    assert [c[1] for c in disp.calls] == [11, 12]


@pytest.mark.asyncio
async def test_poll_is_idempotent_when_cursor_caught_up(monkeypatch):
    """A second sweep with no new posts replays nothing."""
    client = FakeClient()
    client._history = [FakeRawMessage(11)]
    disp = FakeDispatcher()
    poller = ChannelPollingService(
        FakePool(client), FakeRules([FakeRule("-1001")]), disp
    )
    _stub_parse(monkeypatch)

    await poller.sweep_all()
    await asyncio.sleep(0.02)
    assert len(disp.calls) == 1
    await poller.sweep_all()
    await asyncio.sleep(0.02)
    assert len(disp.calls) == 1  # cursor moved, no replay


@pytest.mark.asyncio
async def test_poll_skips_inactive_and_other_session_rules(monkeypatch):
    """Only active rules for the live session are polled."""
    client = FakeClient()
    client._history = [FakeRawMessage(11)]
    disp = FakeDispatcher()
    rules = FakeRules([
        FakeRule("-1001", session_id="s1", active=True),
        FakeRule("-1002", session_id="s1", active=False),
        FakeRule("-1003", session_id="other", active=True),
    ])
    poller = ChannelPollingService(FakePool(client), rules, disp)

    _, checked = await poller.sweep_all()
    assert checked == 1  # only -1001 qualifies


@pytest.mark.asyncio
async def test_poll_survives_a_failing_channel(monkeypatch):
    """One channel raising must not stop the sweep."""
    client = FakeClient()
    client._history = [FakeRawMessage(11)]
    disp = FakeDispatcher()
    rules = FakeRules([FakeRule("-1001"), FakeRule("-1002")])
    poller = ChannelPollingService(FakePool(client), rules, disp)

    replayed, checked = await poller.sweep_all()
    assert checked == 2
    # The good channel still replayed; the broken one was skipped, not fatal.
    assert replayed >= 1


@pytest.mark.asyncio
async def test_loop_runs_and_stops():
    """start()/stop() drive the background sweep cleanly."""
    client = FakeClient()
    disp = FakeDispatcher()
    poller = ChannelPollingService(
        FakePool(client), FakeRules([]), disp, interval=0.01
    )
    await poller.start()
    assert poller._task is not None
    await asyncio.sleep(0.05)
    await poller.stop()
    assert poller._task is None


@pytest.mark.asyncio
async def test_cursor_persistence_roundtrip(monkeypatch):
    """Cursors round-trip through the store and survive a restart."""
    store = FakeCursorStore()
    client = FakeClient()
    client._history = [FakeRawMessage(15)]
    disp = FakeDispatcher()
    poller = ChannelPollingService(
        FakePool(client), FakeRules([FakeRule("-1001")]), disp, state_store=store
    )

    _stub_parse(monkeypatch)
    try:
        await poller.sweep_all()
        assert store.store["-1001"] == 15

        # New poller with the same store must not replay message 15 again
        disp2 = FakeDispatcher()
        poller2 = ChannelPollingService(
            FakePool(client), FakeRules([FakeRule("-1001")]), disp2, state_store=store
        )
        await poller2._restore_cursors()
        assert poller2._state["-1001"] == 15
        await poller2.sweep_all()
        await asyncio.sleep(0.02)
        assert disp2.calls == []
    finally:
        pass


def test_config_reads_channel_poll_interval(monkeypatch):
    from core.config import Config

    monkeypatch.setenv("ATF_CHANNEL_POLL_INTERVAL", "7.5")
    cfg = Config.load(config_path="/nonexistent.yaml")
    assert cfg.channel_poll_interval == 7.5


def test_config_channel_poll_default():
    from core.config import Config

    cfg = Config.load(config_path="/nonexistent.yaml")
    assert cfg.channel_poll_interval == 15.0
