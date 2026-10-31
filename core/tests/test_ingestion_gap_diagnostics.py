"""Ingestion-gap diagnostics: every gap must carry a queryable *cause*.

The mission's §7 distinction — never label all of these as "missing message":

* a Telegram message that genuinely does not exist;
* one that was inaccessible;
* one omitted from a partial API response;
* one rejected by our parser;
* one persisted but not dispatched;
* one dispatched but not delivered;
* one whose outcome is unknown.

These tests pin the first and the third (the two the poller owns) plus the
guarantee that a gap event reaches the **persisted** store, so its cause
survives the live log stream and is queryable per source message.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from pyrogram.errors import ChannelPrivate

from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDiagnosticEventRepository,
    SqliteIngestionGapRepository,
)
from core.infrastructure.telegram.channel_polling import (
    ChannelPollingService, GAP_MAX_ATTEMPTS,
)


class NothingReturns:
    """A difference that returns nothing — the deleted/service-post case."""

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
            return SimpleNamespace(new_messages=[], users=[], chats=[])
        raise RuntimeError(f"unexpected invoke: {type(request).__name__}")

    async def get_chat_history(self, chat_id, limit=1):
        yield SimpleNamespace(id=self._top)


class FailsWithChannelPrivate:
    """A difference call that raises — the inaccessible-channel case."""

    def __init__(self, top_message_id: int):
        self._top = top_message_id

    async def resolve_peer(self, chat_id):
        return SimpleNamespace(id=int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id)

    async def invoke(self, request):
        from pyrogram.raw.functions.channels import GetFullChannel
        from pyrogram.raw.functions.updates import GetChannelDifference
        if isinstance(request, GetFullChannel):
            return SimpleNamespace(full_chat=SimpleNamespace(pts=1000))
        if isinstance(request, GetChannelDifference):
            raise ChannelPrivate("400 CHANNEL_PRIVATE")
        raise RuntimeError(f"unexpected invoke: {type(request).__name__}")

    async def get_chat_history(self, chat_id, limit=1):
        yield SimpleNamespace(id=self._top)


class Pool:
    def __init__(self, client):
        self._client = client

    def get(self, session_id):
        return self._client


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "gapdiag.db"))


@pytest.fixture
def gaps(db):
    return SqliteIngestionGapRepository(db)


@pytest.fixture
def diag(db):
    return SqliteDiagnosticEventRepository(db)


def _poller(gaps, diag, client):
    return ChannelPollingService(
        pool=Pool(client), rules=None, dispatcher=None, gap_repo=gaps,
        diagnostics_repo=diag, interval=2.0,
    )


async def _sweep(poller, client, chat_id="-1004400615270"):
    await asyncio.wait_for(
        poller._poll_channel(client, "sess", chat_id, "test"), timeout=5.0)
    await asyncio.sleep(0.1)


async def test_a_created_gap_is_persisted_with_its_cause(db, gaps, diag):
    """A truncated difference produces a queryable event, not just a log line."""
    client = NothingReturns(top_message_id=103)
    poller = _poller(gaps, diag, client)
    poller._state["-1004400615270"] = 100

    await _sweep(poller, client)

    events = await diag.query_events(source_peer_id="-1004400615270")
    assert events, "a created gap must persist a diagnostic event"
    row = events[0]
    assert row["event_name"] == "ingestion.ingestion_gap_created"
    assert row["stage"] == "INGEST"
    # Cause: a partial API response, i.e. we asked and Telegram served fewer.
    assert row["reason"] == "INGESTION_GAP_DETECTED"
    assert row["category"]


async def test_an_exhausted_gap_means_the_message_does_not_exist(db, gaps, diag):
    """Exhausting the budget is Telegram's terminal answer, not an unknown.

    The sweep asked five times; the id was never served. That is the
    *does-not-exist* class (deleted or service post) — a terminal answer the
    mission requires to be named, not bundled into "missing".
    """
    client = NothingReturns(top_message_id=103)
    poller = _poller(gaps, diag, client)

    await gaps.record_gaps("sess", "-1004400615270", [101])
    await gaps.mark_gap_attempt(
        "gap:sess:-1004400615270:101", "id not returned", 0, exhausted=True)

    # Drive the persistence path directly through the poller's emitter.
    poller._emit("error", "INGESTION_GAP_EXHAUSTED",
                 "channel -1004400615270: id=101 not returned after 5 attempt(s)",
                 {"chat_id": "-1004400615270", "session_id": "sess",
                  "source_message_id": 101, "attempts": 5,
                  "raw_error": "ID_NOT_RETURNED"})
    await asyncio.sleep(0.1)

    events = await diag.query_events(source_peer_id="-1004400615270")
    assert events, "an exhausted gap must be persisted"
    row = [e for e in events if e["event_name"] == "ingestion.ingestion_gap_exhausted"][0]
    assert row["reason"] == "INGESTION_MESSAGE_NOT_EXIST"
    assert row["raw_rpc_id"] == "ID_NOT_RETURNED"


async def test_a_recovery_failure_keeps_the_raw_telegram_error(db, gaps, diag):
    """An inaccessible channel keeps Telegram's own error id verbatim."""
    client = FailsWithChannelPrivate(top_message_id=103)
    poller = _poller(gaps, diag, client)

    poller._emit("error", "INGESTION_GAP_PERMANENT_FAILURE",
                 "channel -1004400615270: id=101 permanently unrecoverable",
                 {"chat_id": "-1004400615270", "session_id": "sess",
                  "source_message_id": 101, "attempts": 5,
                  "raw_error": "CHANNEL_PRIVATE"})
    await asyncio.sleep(0.1)

    events = await diag.query_events(source_peer_id="-1004400615270")
    assert events
    row = [e for e in events if e["event_name"] == "ingestion.ingestion_gap_permanent_failure"][0]
    # The origin says Telegram answered, and the raw layer says what it said.
    assert row["error_origin"] == "TELEGRAM"
    assert row["category"] == "TELEGRAM_API"
    assert row["raw_rpc_id"] == "CHANNEL_PRIVATE"


async def test_a_recovered_gap_is_recorded_as_recovered(db, gaps, diag):
    """Recovery is a positive outcome, not just the absence of a failure."""
    client = NothingReturns(top_message_id=103)
    poller = _poller(gaps, diag, client)

    poller._emit("info", "INGESTION_GAP_RECOVERED",
                 "channel -1004400615270: id=101 recovered",
                 {"chat_id": "-1004400615270", "session_id": "sess",
                  "source_message_id": 101, "raw_error": ""})
    await asyncio.sleep(0.1)

    events = await diag.query_events(source_peer_id="-1004400615270")
    assert events
    row = [e for e in events if e["event_name"] == "ingestion.ingestion_gap_recovered"][0]
    assert row["reason"] == "INGESTION_GAP_RECOVERED"
    assert row["severity"] == "INFO"


async def test_a_poller_without_the_store_still_polls(db, gaps):
    """The diagnostics store is optional: no wiring, no failure, same poll."""
    client = NothingReturns(top_message_id=103)
    poller = ChannelPollingService(
        pool=Pool(client), rules=None, dispatcher=None, gap_repo=gaps, interval=2.0)
    poller._state["-1004400615270"] = 100
    # First sweep: the ids are open gaps, so the cursor correctly holds at 100
    # (this is the reliability contract — no silent advancement).
    await _sweep(poller, client)
    assert poller._state["-1004400615270"] == 100
    # Mark them terminal (the recovery budget spent): the sweep now advances,
    # and the poller kept working the whole time with no diagnostics store.
    for mid in (101, 102, 103):
        await gaps.mark_gap_attempt(f"gap:sess:-1004400615270:{mid}", "gone",
                                    0, exhausted=True)
    await _sweep(poller, client)
    assert poller._state["-1004400615270"] == 103
