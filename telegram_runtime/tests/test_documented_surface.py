"""Contract tests for the two documented-surface guards in the pyrogram
adapter — the calls the Go Worker resolves through the gateway.

pyrogram 2.0.106 (the pinned release) is the reference for both:

* ``get_album_message_ids`` must answer through ``get_media_group`` — the
  documented one-to-one method for "which messages share this album". The
  previous implementation called ``get_messages(limit=...)``, and ``limit``
  is not a parameter the pinned release accepts (it is a Bot-API concept):
  the call failed on every album and the worker degraded to single copies.
* topic targeting must thread ``message_thread_id`` only when the installed
  client's method signature accepts it. The pinned release's
  forward/copy/send methods predate the parameter; passing it anyway raised
  TypeError and failed every topic-targeted delivery.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_runtime.adapters.pyrogram.client import PyrogramAdapter  # noqa: E402
from core.infrastructure.telegram.pyrogram_compat import thread_kwargs  # noqa: E402


class _AlbumClient:
    """pyrogram-shaped: get_media_group answers, get_messages records."""

    def __init__(self, members: list) -> None:
        self._members = members
        self.calls: list = []

    async def get_messages(self, chat_id=None, message_ids=None):
        # The pinned signature: NO `limit` parameter exists. If the adapter
        # regresses to the old window call, this raises TypeError — and the
        # test catches the fallback instead of the documented method.
        self.calls.append(("get_messages", chat_id, message_ids))
        raise TypeError(
            "get_messages() got an unexpected keyword argument 'limit'")

    async def get_media_group(self, chat_id=None, message_id=None):
        self.calls.append(("get_media_group", chat_id, message_id))
        return self._members


class _ThreadedClient:
    """A fork-shaped client: every send method accepts message_thread_id."""

    def __init__(self) -> None:
        self.calls: list = []

    def _accepts(self, *_, **__):
        pass

    async def forward_messages(self, chat_id=None, from_chat_id=None,
                               message_ids=None, message_thread_id=None):
        self.calls.append(("forward_messages", chat_id, from_chat_id, message_ids, message_thread_id))
        # pyrogram returns one Message per id (a list for a multi-id call).
        return [SimpleNamespace(id=111), SimpleNamespace(id=112)]

    async def copy_message(self, chat_id=None, from_chat_id=None,
                           message_id=None, message_thread_id=None,
                           caption=None, reply_markup=None):
        self.calls.append(("copy_message", chat_id, message_thread_id))
        return SimpleNamespace(id=222)

    async def send_message(self, chat_id=None, text=None, message_thread_id=None):
        self.calls.append(("send_message", chat_id, text, message_thread_id))
        return SimpleNamespace(id=333)


class _PinnedClient:
    """The pinned 2.0.106 shape: no message_thread_id anywhere."""

    def __init__(self) -> None:
        self.calls: list = []

    async def forward_messages(self, chat_id=None, from_chat_id=None, message_ids=None):
        self.calls.append(("forward_messages", message_ids))
        return [SimpleNamespace(id=111)]

    async def copy_message(self, chat_id=None, from_chat_id=None,
                           message_id=None, caption=None, reply_markup=None):
        self.calls.append(("copy_message", message_id))
        return SimpleNamespace(id=222)

    async def send_message(self, chat_id=None, text=None):
        self.calls.append(("send_message", text))
        return SimpleNamespace(id=333)


# --- get_album_message_ids uses the documented get_media_group --------------
def test_album_resolution_calls_get_media_group_not_get_messages():
    client = _AlbumClient([SimpleNamespace(id=5), SimpleNamespace(id=6)])
    adapter = PyrogramAdapter(client)
    # The stub message answers with a media_group_id so the resolver must go
    # looking for the album members.
    source = SimpleNamespace(id=5, media_group_id="album-1")
    async def fake_get_message(chat_id, message_id):
        return source, None
    adapter.get_message = fake_get_message  # type: ignore[method-assign]

    ids, ferr = asyncio.run(adapter.get_album_message_ids("-100A", 5))
    assert ferr is None
    assert ids == [5, 6]
    assert ("get_media_group", "-100A", 5) in client.calls
    assert not any(name == "get_messages" for name, *_ in client.calls), (
        "the adapter must resolve albums through the documented "
        "get_media_group, not a hand-rolled get_messages window")


# --- topic targeting rides the documented surface ---------------------------
def test_topic_kwarg_is_threaded_when_the_client_accepts_it():
    client = _ThreadedClient()
    adapter = PyrogramAdapter(client)
    sent, ferr = asyncio.run(adapter.forward_messages("-100B", "-100A", [1, 2], 77))
    assert ferr is None and sent == [111, 112]
    assert client.calls[0][-1] == 77


def test_topic_kwarg_is_dropped_when_the_pinned_surface_lacks_it():
    client = _PinnedClient()
    adapter = PyrogramAdapter(client)
    # On the pinned release this used to raise TypeError inside the adapter —
    # a topic-targeted task failed every delivery. The kwarg is now dropped
    # and the message lands in the chat's General topic.
    ids, ferr = asyncio.run(adapter.forward_messages("-100B", "-100A", [1, 2], 77))
    assert ferr is None and ids == [111]
    assert len(client.calls) == 1

    sent, ferr = asyncio.run(adapter.copy_message("-100B", "-100A", 5, None, 77))
    assert ferr is None and sent == 222
    sent, ferr = asyncio.run(adapter.send_message("-100B", "hello", 77))
    assert ferr is None and sent == 333


def test_thread_kwargs_helper_semantics():
    threaded, pinned = _ThreadedClient(), _PinnedClient()
    assert thread_kwargs(threaded, "forward_messages", 7) == {"message_thread_id": 7}
    assert thread_kwargs(pinned, "forward_messages", 7) == {}
    # zero/absent topic: nothing, on either shape
    assert thread_kwargs(threaded, "forward_messages", 0) == {}
    assert thread_kwargs(pinned, "forward_messages", None) == {}
    # a multi-method dict is included only when EVERY method accepts it
    assert thread_kwargs(pinned, ["forward_messages", "copy_message"], 7) == {}
    both = _ThreadedClient()
    assert thread_kwargs(both, ["forward_messages", "copy_message", "send_message"], 7) == {
        "message_thread_id": 7,
    }
