"""The poller must not hold its cursor past a *terminal* gap.

**The production failure this pins.** After PR #274, a channel whose ids
`GetChannelDifference` never returns (deleted or service posts) entered a
permanent loop:

1. cursor = 100, channel top = 103 → poller asks for ``101..103``;
2. Telegram returns nothing → all three ids become `ingestion_gaps` rows;
3. the gap sweep retries each one, and after ``GAP_MAX_ATTEMPTS`` marks it
   ``FAILED`` — *"id not returned by Telegram (deleted or service post)"*;
4. the poller's truncation branch held the cursor whenever *any* gap existed,
   without checking whether that gap was still open;
5. so it asked for ``101..103`` again every sweep, and logged the same
   ``[POLL GAP] … recorded as durable recovery work`` warning every 15 s,
   **forever**.

The terminal states are *accounted for*: a ``RESOLVED`` row means the post was
delivered, a ``FAILED`` row means the recovery budget was spent on a post that
does not exist (no delivery is owed). Neither may block the cursor; only a
still-open gap (``PENDING``/``RETRYING``) may.

This test drives the real poller against the real repositories, with a fake
client whose difference returns nothing — the exact production condition — and
asserts the cursor advances and the warning is emitted **once**.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteIngestionGapRepository,
)
from core.infrastructure.telegram.channel_polling import (
    ChannelPollingService, GAP_MAX_ATTEMPTS,
)


class NothingReturns:
    """A client whose channel difference always returns zero messages.

    That is exactly what Telegram does for a deleted or service post id, and
    it is the production condition: the poller asks, nothing comes back.
    """

    def __init__(self, top_message_id: int):
        self._top = top_message_id
        self.difference_calls = 0

    async def resolve_peer(self, chat_id):
        return SimpleNamespace(id=int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id)

    async def invoke(self, request):
        from pyrogram.raw.functions.channels import GetFullChannel
        from pyrogram.raw.functions.updates import GetChannelDifference
        if isinstance(request, GetFullChannel):
            return SimpleNamespace(full_chat=SimpleNamespace(pts=1000))
        if isinstance(request, GetChannelDifference):
            self.difference_calls += 1
            # Nothing at the requested ids — the deleted-post case.
            return SimpleNamespace(new_messages=[], users=[], chats=[])
        raise RuntimeError(f"unexpected invoke: {type(request).__name__}")

    async def get_chat_history(self, chat_id, limit=1):
        # The poller drives this with `async for`, so it must *be* an async
        # generator, not a function returning one.
        yield SimpleNamespace(id=self._top)


class Pool:
    def __init__(self, client):
        self._client = client

    def get(self, session_id):
        return self._client


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "gaploop.db"))


@pytest.fixture
def gaps(db):
    return SqliteIngestionGapRepository(db)


def _poller(gaps, client, **kw):
    return ChannelPollingService(
        pool=Pool(client), rules=None, dispatcher=None,
        gap_repo=gaps, interval=2.0, **kw,
    )


async def _sweep_once(poller, client, chat_id="-1004400615270"):
    """Run one poll cycle for the channel and let its tasks finish."""
    task = asyncio.create_task(poller._poll_channel(client, "sess", chat_id, "test"))
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except asyncio.TimeoutError:
        task.cancel()
    await asyncio.sleep(0.05)


async def test_the_cursor_advances_past_an_exhausted_gap(db, gaps, caplog):
    """A FAILED gap must not block the cursor — the work is done."""
    client = NothingReturns(top_message_id=103)
    poller = _poller(gaps, client)

    # Pretend the cursor is at 100 and ids 101..103 already exhausted their
    # budget: the exact production state.
    await gaps.record_gaps("sess", "-1004400615270", [101, 102, 103])
    for mid in (101, 102, 103):
        await gaps.mark_gap_attempt(
            f"gap:sess:-1004400615270:{mid}",
            "id not returned by Telegram (deleted or service post)",
            0, exhausted=True,
        )

    poller._state["-1004400615270"] = 100
    await _sweep_once(poller, client)

    assert poller._state["-1004400615270"] == 103, (
        f"the cursor must advance past exhausted gaps, got {poller._state['-1004400615270']}"
    )


async def test_the_cursor_holds_at_a_still_open_gap(db, gaps, caplog):
    """A PENDING gap still owns its id: the cursor may not pass it."""
    client = NothingReturns(top_message_id=103)
    poller = _poller(gaps, client)

    await gaps.record_gaps("sess", "-1004400615270", [102])
    poller._state["-1004400615270"] = 100
    await _sweep_once(poller, client)

    assert poller._state["-1004400615270"] == 100, (
        f"an open gap must hold the cursor, got {poller._state['-1004400615270']}"
    )


async def test_a_newly_missing_id_is_recorded_once_not_every_sweep(db, gaps, caplog):
    """The truncation warning fires for a *new* gap, then goes quiet."""
    client = NothingReturns(top_message_id=103)
    poller = _poller(gaps, client)
    poller._state["-1004400615270"] = 100

    with caplog.at_level(logging.WARNING):
        await _sweep_once(poller, client)
        await _sweep_once(poller, client)
        await _sweep_once(poller, client)

    warnings = [r for r in caplog.records if "new id(s) missing" in r.getMessage()]
    assert len(warnings) == 1, (
        f"a recurring truncation must warn once, not every sweep; saw {len(warnings)}"
    )


async def test_record_gaps_returns_the_genuinely_new_count(gaps):
    """The repository distinguishes a new gap from one already known."""
    assert await gaps.record_gaps("sess", "-1004400615270", [10, 11]) == 2
    # The same ids again: nothing is new.
    assert await gaps.record_gaps("sess", "-1004400615270", [10, 11]) == 0
    # A mix: one new, one known.
    assert await gaps.record_gaps("sess", "-1004400615270", [11, 12]) == 1


async def test_open_gaps_excludes_terminal_states(gaps):
    """RESOLVED and FAILED are accounted for; only pending work is open."""
    await gaps.record_gaps("sess", "-1004400615270", [10, 11, 12])
    await gaps.mark_gap_attempt("gap:sess:-1004400615270:10", "gone", 0, exhausted=True)
    await gaps.resolve_gap("sess", "-1004400615270", 11)

    open_ids = [int(r["source_message_id"]) for r in await gaps.open_gaps("sess", "-1004400615270")]
    assert open_ids == [12], f"only the still-open id should remain, got {open_ids}"


async def test_open_gaps_scopes_below_a_top(gaps):
    """The ``below`` bound keeps the poller's read narrow."""
    await gaps.record_gaps("sess", "-1004400615270", [10, 20, 30])
    assert [int(r["source_message_id"]) for r in await gaps.open_gaps("sess", "-1004400615270", below=20)] == [10, 20]
