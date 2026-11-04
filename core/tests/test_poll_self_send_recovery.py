"""Regression: an empty GetChannelDifference must not hide real posts.

**The production failure this pins.** The poller passes the channel's
*current* pts (from GetFullChannel) to GetChannelDifference, which is the
pts a client supplies when it has ALREADY seen everything. Any post written
through this very session — an operator probe, a same-account tool, any
client sharing the auth key — is covered by that pts, so Telegram answers
``ChannelDifferenceEmpty`` even though the post exists, the history read
returns it, and ``top > cursor`` proves the sweep had work to do.

The live update loop agrees with the difference: it also believes the
session already knows its own write. So for those posts the empty
difference is the *only* path — and it was classifying them as
``ingestion_gaps`` (eventually FAILED — "post is most likely deleted"),
silently dropping every one.

The poller now falls back to a plain history read of the window, which
carries no pts gate, and dispatches what it finds. This test drives the
real poller against the real repositories with a fake client whose
difference is EMPTY while its history read returns the post — the exact
production condition — and asserts the post reaches the dispatcher and the
cursor advances.
"""

from __future__ import annotations

import asyncio

import pytest

from core.infrastructure.telegram.channel_polling import (
    ChannelPollingService,
)

from .test_v15_channel_polling import (
    FakeClient,
    FakeDifference,
    FakeDispatcher,
    FakePool,
    FakeRawMessage,
    FakeRule,
    FakeRules,
    _stub_parse,
)


class EmptyDifference:
    """What Telegram answers when the pts gate hides the whole window."""

    new_messages = []
    users = []
    chats = []


@pytest.mark.asyncio
async def test_poll_recovers_posts_the_pts_gate_hides(monkeypatch):
    """An empty difference + a post in the window must dispatch it."""
    client = FakeClient()
    client._history = [FakeRawMessage(22), FakeRawMessage(21)]

    async def invoke(query):
        name = type(query).__name__
        if name == "GetFullChannel":
            from .test_v15_channel_polling import _FakeFullChat

            return _FakeFullChat()
        if name == "GetChannelDifference":
            return EmptyDifference()
        return None

    client.invoke = invoke
    disp = FakeDispatcher()
    poller = ChannelPollingService(FakePool(client), FakeRules([FakeRule("-1001")]), disp)
    _stub_parse(monkeypatch)

    replayed, checked = await poller.sweep_all()
    assert checked == 1
    _ = replayed

    # The dispatch is scheduled onto the loop; give it a tick to run.
    await asyncio.sleep(0.05)
    assert [c[1] for c in disp.calls] == [21, 22], disp.calls
    # The cursor must advance past the dispatched ids or the next sweep
    # re-asks for them forever (the original failure mode).
    assert poller._state.get("-1001", 0) >= 22


@pytest.mark.asyncio
async def test_poll_empty_difference_and_empty_window_records_nothing(monkeypatch):
    """An empty difference over a genuinely empty window stays quiet."""
    client = FakeClient()
    client._history = []  # the history read also sees nothing
    client._difference = EmptyDifference()

    async def invoke(query):
        name = type(query).__name__
        if name == "GetFullChannel":
            from .test_v15_channel_polling import _FakeFullChat

            return _FakeFullChat()
        if name == "GetChannelDifference":
            return EmptyDifference()
        return None

    client.invoke = invoke
    disp = FakeDispatcher()
    poller = ChannelPollingService(FakePool(client), FakeRules([FakeRule("-1001")]), disp)
    _stub_parse(monkeypatch)

    replayed, checked = await poller.sweep_all()
    assert checked == 1
    await asyncio.sleep(0.05)
    assert disp.calls == []
    # No cursor movement when nothing dispatched.
    assert poller._state.get("-1001", 0) == 0


# The FakeClient in the sibling stock uses a FakeDifference that echoes
# history; referenced here so the import stays meaningful for readers.
_ = FakeDifference
