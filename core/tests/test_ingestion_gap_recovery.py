"""Regression tests for the ingestion-gap recovery (audit findings #1 and #2).

Finding #1 — a truncated ``GetChannelDifference`` left the poller's cursor past
ids Telegram never returned. Production evidence: source ``-1004400615270``
with cursor=100 while ids 72, 83, 86–96 and 100 had no mapping, no delivery
job, no processed marker and no log line — 13 posts lost permanently, with the
poller reporting "nothing newer than the cursor" forever after.

Finding #2 — the replay lane swallowed every dispatch exception while the
cursor still advanced to the last returned id, so a post that failed parsing
was skipped permanently too.

The tests drive the real ``ChannelPollingService`` and the real
``SqliteIngestionGapRepository`` with a fake Telegram that truncates on demand.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import SqliteIngestionGapRepository
from core.infrastructure.telegram.channel_polling import ChannelPollingService

CHAT = "-1004400615270"
SESSION = "sess"


def make_raw_message(i: int):
    """A minimal but genuine pyrogram raw Message the lane can _parse."""
    from pyrogram.raw.types import Channel as RawChannel, Message as RawMessage, PeerChannel

    return RawMessage(
        id=i,
        peer_id=PeerChannel(channel_id=4400615270),
        from_id=None,
        message=f"post {i}",
        entities=[],
        date=0,
    )


class FakeDiff:
    def __init__(self, msgs, chats):
        self.new_messages = msgs
        self.users = []
        self.chats = chats


class FakeTelegram:
    """Emulates a channel the difference endpoint truncates for."""

    def __init__(self, real_ids, returned_ids):
        self.real_ids = list(real_ids)
        self.returned_ids = list(returned_ids)
        self.dispatched: list = []
        self.message_cache = {}
        self._chats = [RawChannel(id=4400615270, title="A", access_hash=0,
                                  photo=None, date=0, restriction_reason=[])]

    async def resolve_peer(self, x):
        return x

    async def invoke(self, req):
        name = type(req).__name__
        if name == "GetFullChannel":
            return types.SimpleNamespace(full_chat=types.SimpleNamespace(pts=103))
        if name == "GetChannelDifference":
            asked_lo = req.filter.ranges[0].min_id
            asked_hi = req.filter.ranges[0].max_id
            if asked_lo == asked_hi:
                # A single-id recovery probe: answer only if that id is real
                # *and* was part of the originally-returned set is too strict —
                # recovery is precisely for the ids the difference omitted.
                if asked_lo in self.real_ids:
                    return FakeDiff([make_raw_message(asked_lo)], self._chats)
                return FakeDiff([], self._chats)
            rng = [i for i in self.returned_ids if asked_lo <= i <= asked_hi]
            return FakeDiff([make_raw_message(i) for i in sorted(rng)], self._chats)
        raise AssertionError(name)

    def get_chat_history(self, target, limit=1):
        top = max(self.real_ids) if self.real_ids else 0
        async def gen():
            if top:
                yield make_raw_message(top)
        return gen()


from pyrogram.raw.types import Channel as RawChannel  # noqa: E402


class FakeDispatcher:
    def __init__(self, sink: list):
        self._sink = sink

    def message_handler(self, sid):
        async def h(c, m):
            self._sink.append(m.id)
        return h


class FakeCursorStore:
    def __init__(self):
        self.cursors = {}

    async def load_channel_cursors(self):
        return dict(self.cursors)

    async def save_channel_cursors(self, c):
        self.cursors.update(c)


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "gaps.db"))


@pytest.fixture
def gaps(db):
    return SqliteIngestionGapRepository(db)


def make_poller(client, gaps, cursor=0):
    p = ChannelPollingService(pool=None, rules=None, dispatcher=FakeDispatcher(client.dispatched),
                              state_store=FakeCursorStore(), interval=15.0, gap_repo=gaps)
    p._state[CHAT] = cursor
    p._pool = types.SimpleNamespace(get=lambda sid: client)
    return p


# --------------------------------------------------------------------------- #
# Finding #1: a truncated difference must not advance the cursor past the gap
# --------------------------------------------------------------------------- #

async def test_a_truncated_difference_records_every_unreturned_id(db, gaps):
    """The production pattern exactly: source 61..100, difference returns
    {84,85,92,97,98,99}. Every other id must become durable recovery work."""
    real = list(range(61, 101))
    returned = [84, 85, 92, 97, 98, 99]
    client = FakeTelegram(real, returned)
    poller = make_poller(client, gaps, cursor=60)

    await poller._poll_channel(client, SESSION, CHAT, "A")
    await asyncio.sleep(0.4)

    pending = await gaps.pending_gaps(SESSION, CHAT)
    pending_ids = sorted(g["source_message_id"] for g in pending)
    assert pending_ids == sorted(set(real) - set(returned))
    assert sorted(client.dispatched) == returned


async def test_the_cursor_advances_only_over_ids_the_pipeline_accepted(db, gaps):
    """Before the fix the cursor jumped to `top` (or to the last returned id)
    and the missing ids were never re-requested."""
    client = FakeTelegram(list(range(61, 101)), [84, 85, 92, 97, 98, 99])
    poller = make_poller(client, gaps, cursor=60)

    await poller._poll_channel(client, SESSION, CHAT, "A")
    await asyncio.sleep(0.4)

    # The pipeline accepted exactly the returned ids; the cursor cannot run
    # past the highest one of those.
    assert poller._state[CHAT] == 99
    # Id 100 was NOT returned, so the next sweep must not consider it seen.
    assert poller._state[CHAT] < 100


async def test_a_recovered_gap_is_dispatched_and_resolved(db, gaps):
    """The gap sweep re-fetches the id with a narrow range; when Telegram
    returns it, the post goes through the pipeline and the gap is resolved."""
    client = FakeTelegram(list(range(61, 101)), [84, 85, 92, 97, 98, 99])
    poller = make_poller(client, gaps, cursor=60)
    await poller._poll_channel(client, SESSION, CHAT, "A")
    await asyncio.sleep(0.4)
    before = sorted(client.dispatched)

    gaps._db.execute(
        "UPDATE ingestion_gaps SET next_retry_at = 0 WHERE state IN ('PENDING','RETRYING')"
    )
    recovered = await poller._recover_gaps(limit=100)
    await asyncio.sleep(0.4)

    assert recovered > 0
    assert sorted(client.dispatched) != before, "no recovered post was dispatched"
    # Every real id has now been seen by the pipeline.
    assert set(client.dispatched) == set(range(61, 101))


async def test_an_id_that_no_longer_exists_fails_visibly_not_silently(db, gaps):
    """A gap that recovery cannot serve (a deleted post) consumes a bounded
    retry budget and ends FAILED with an actionable reason — never a silent
    drop and never an infinite loop."""
    client = FakeTelegram(list(range(61, 101)), [84])
    poller = make_poller(client, gaps, cursor=60)
    await poller._poll_channel(client, SESSION, CHAT, "A")
    await asyncio.sleep(0.3)

    # Make every probe fail by emptying the channel: the ids are "deleted".
    client.real_ids = []
    for _ in range(8):
        gaps._db.execute(
            "UPDATE ingestion_gaps SET next_retry_at = 0 WHERE state IN ('PENDING','RETRYING')"
        )
        await poller._recover_gaps(limit=200)
        await asyncio.sleep(0.05)

    pending = await gaps.pending_gaps(SESSION, CHAT)
    states = {g["state"] for g in pending}
    assert states == {"FAILED"}, f"unrecoverable gaps must end FAILED, got {states}"
    assert all(g["last_error"] for g in pending), "a FAILED gap carries its reason"


async def test_the_pipeline_resolves_a_gap_when_it_sees_the_id(db, gaps):
    """The other half of the loop: the ingest path closes a gap row the moment
    the pipeline receives the source id — before any filtering can hide it."""
    await gaps.record_gaps(SESSION, CHAT, [61, 62, 63])
    assert len(await gaps.pending_gaps(SESSION, CHAT)) == 3

    # Simulate the pipeline's ingest stage for one of them.
    resolved = await gaps.resolve_gap_for_source(CHAT, 62)
    assert resolved is True

    pending = await gaps.pending_gaps(SESSION, CHAT)
    assert sorted(g["source_message_id"] for g in pending) == [61, 63]


async def test_a_failed_gap_survives_resolution(db, gaps):
    """A FAILED gap (budget spent) is the actionable record of an unrecoverable
    id; resolving it when the pipeline later sees the post would erase the audit
    trail — so resolution only ever clears non-terminal rows."""
    await gaps.record_gaps(SESSION, CHAT, [70])
    await gaps.mark_gap_attempt("gap:%s:%s:70" % (SESSION, CHAT), "never returned", 0.0,
                                 exhausted=True)
    resolved = await gaps.resolve_gap_for_source(CHAT, 70)
    assert resolved is False
    rows = await gaps.pending_gaps(SESSION, CHAT)
    assert rows and rows[0]["state"] == "FAILED"


# --------------------------------------------------------------------------- #
# Finding #2: a lane failure must not let the cursor advance
# --------------------------------------------------------------------------- #

async def test_a_failed_dispatch_does_not_advance_the_cursor(db, gaps):
    """Every post fails dispatch (the lane worker raises on each one) and the
    cursor must refuse to advance past any of them."""
    client = FakeTelegram(list(range(61, 71)), list(range(61, 71)))
    poller = make_poller(client, gaps, cursor=60)

    # A dispatcher whose handler raises for every post.
    class ExplodingDispatcher(FakeDispatcher):
        def message_handler(self, sid):
            async def h(c, m):
                raise RuntimeError("ingestion deliberately failed")
            return h

    poller._dispatcher = ExplodingDispatcher([])

    await poller._poll_channel(client, SESSION, CHAT, "A")
    await asyncio.sleep(0.4)

    assert client.dispatched == []
    assert poller._state.get(CHAT, 60) <= 60, (
        "the cursor advanced past posts the pipeline never accepted"
    )
    # And those posts became recovery work rather than vanishing.
    pending = await gaps.pending_gaps(SESSION, CHAT)
    assert sorted(g["source_message_id"] for g in pending) == list(range(61, 71))
