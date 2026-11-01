"""gRPC surface tests for the v33 diagnostic-event query API.

The monitoring contract (mission §10) says an operator must be able to open one
failed message and see the whole story — the raw Telegram error, the normalized
category, every retry and fallback. These tests prove that surface against the
real servicer and the real repository, and specifically that:

* the per-message timeline comes back oldest-first and complete;
* the raw Telegram layer survives the wire (RPC code, id, wait value);
* an internal failure is classified as INTERNAL, never faked as Telegram;
* **tenant isolation is structural**: one owner cannot query another's events;
* a Core without the store (pre-v33) still serves empty results, not errors.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pyrogram.errors import ChatForwardsRestricted, FloodWait

from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDiagnosticEventRepository,
)
from core.infrastructure.telegram.diagnostics_contract import (
    C_DELIVERY, C_TELEGRAM_API, ORIGIN_INTERNAL, S_COPY, S_NATIVE_FORWARD,
    build_diagnostic_event,
)


# --------------------------------------------------------------------------- #
# A servicer wired the way production wires it: only the pieces this surface
# needs, so the test proves the query path does not depend on the rest.
# --------------------------------------------------------------------------- #

class FakeContext:
    pass


def _make_servicer(diag_repo=None):
    """Build a DeliveryControlServicer with the diagnostic store attached."""
    from core.infrastructure.grpc_server.servicers import DeliveryControlServicer
    servicer = DeliveryControlServicer(db=None, rule_repo=None,
                                       queue_repo=None, reconciler=None)
    # v33 wiring: the store may be absent on an older Core.
    servicer._diagnostic_events = diag_repo
    return servicer


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "grpcdiag.db"))


@pytest.fixture
def repo(db):
    return SqliteDiagnosticEventRepository(db)


async def _seed_retried_delivery(repo, owner=7):
    """A delivery that failed with a restriction, fell back, and succeeded."""
    base = time.time()
    events = []
    # Attempt 1: native forward refused by Telegram (forwards restricted).
    e1 = build_diagnostic_event(
        event_name="delivery.strategy.failed", stage="SEND",
        category=C_TELEGRAM_API, task_id="rule-9", owner_id=owner,
        source_peer_id="-100SRC", source_message_id=4242,
        target_peer_id="-100DST", attempt=1, strategy=S_NATIVE_FORWARD,
        raw_exception=ChatForwardsRestricted("400 CHAT_FORWARDS_RESTRICTED"),
    )
    e1.timestamp = base
    events.append(e1)
    # Attempt 2: the copy fallback also refused.
    e2 = build_diagnostic_event(
        event_name="delivery.strategy.failed", stage="SEND",
        category=C_TELEGRAM_API, task_id="rule-9", owner_id=owner,
        source_peer_id="-100SRC", source_message_id=4242,
        target_peer_id="-100DST", attempt=2, strategy=S_COPY,
        raw_exception=ChatForwardsRestricted("400 CHAT_FORWARDS_RESTRICTED"),
    )
    e2.timestamp = base + 1
    events.append(e2)
    # Attempt 3: delivered after the re-upload fallback.
    e3 = build_diagnostic_event(
        event_name="delivery.completed", stage="SEND",
        category=C_DELIVERY, task_id="rule-9", owner_id=owner,
        source_peer_id="-100SRC", source_message_id=4242,
        target_peer_id="-100DST", attempt=3, strategy="REUPLOAD",
    )
    e3.timestamp = base + 2
    events.append(e3)
    for e in events:
        assert await repo.record_event(e)
    return base


async def test_the_message_timeline_is_complete_and_oldest_first(repo):
    """The drill-down shows every attempt and the original Telegram answer."""
    await _seed_retried_delivery(repo)
    servicer = _make_servicer(repo)

    resp = await servicer.GetMessageTimeline(
        _req(owner=7, source_peer_id="-100SRC", source_message_id=4242),
        FakeContext())

    assert [e.attempt for e in resp.events] == [1, 2, 3]
    # The *first* failure is the native restriction, and it is preserved even
    # though the delivery ultimately succeeded (mission §6.3).
    first = resp.events[0]
    assert first.raw_rpc_id == "CHAT_FORWARDS_RESTRICTED"
    assert first.raw_rpc_code == "400"
    assert first.strategy == S_NATIVE_FORWARD
    assert first.error_origin == "TELEGRAM"
    assert first.category == C_TELEGRAM_API
    # The fallback's own failure is there too — nothing was overwritten.
    assert resp.events[1].strategy == S_COPY
    assert resp.events[2].event_name == "delivery.completed"


async def test_a_floodwait_keeps_telegrams_wait_over_the_wire(repo):
    """The retry-after value Telegram demanded survives the round trip."""
    event = build_diagnostic_event(
        event_name="delivery.flood_wait.retrying", stage="SEND",
        category=C_TELEGRAM_API, task_id="rule-9", owner_id=7,
        source_peer_id="-100SRC", source_message_id=500,
        target_peer_id="-100DST", attempt=1, strategy=S_NATIVE_FORWARD,
        raw_exception=FloodWait(45),
    )
    await repo.record_event(event)
    servicer = _make_servicer(repo)

    resp = await servicer.GetMessageTimeline(
        _req(owner=7, source_peer_id="-100SRC", source_message_id=500),
        FakeContext())

    assert resp.events[0].raw_retry_after == 45
    assert resp.events[0].retryable is True


async def test_an_internal_error_is_not_faked_as_telegram_over_the_wire(repo):
    """Our own failures keep their INTERNAL origin on the wire."""
    event = build_diagnostic_event(
        event_name="parse.failed", stage="PARSE", category="PARSING",
        task_id="rule-9", owner_id=7, source_peer_id="-100SRC",
        source_message_id=600, error_origin=ORIGIN_INTERNAL,
    )
    await repo.record_event(event)
    servicer = _make_servicer(repo)

    resp = await servicer.QueryDiagnosticEvents(
        _query(owner=7, category="PARSING"), FakeContext())

    assert resp.events, "a parse failure must be queryable"
    assert resp.events[0].error_origin == "INTERNAL"
    assert resp.events[0].category == "PARSING"


async def test_one_owner_cannot_query_another_owners_events(repo):
    """Tenant isolation: owner 8's timeline returns nothing for owner 7."""
    await _seed_retried_delivery(repo, owner=8)
    servicer = _make_servicer(repo)

    # Owner 7 asks for owner 8's message.
    resp = await servicer.GetMessageTimeline(
        _req(owner=7, source_peer_id="-100SRC", source_message_id=4242),
        FakeContext())
    assert list(resp.events) == [], "cross-tenant read must return nothing"

    # And owner 8 sees their own.
    own = await servicer.GetMessageTimeline(
        _req(owner=8, source_peer_id="-100SRC", source_message_id=4242),
        FakeContext())
    assert len(own.events) == 3


async def test_the_facet_counts_drive_the_monitoring_views(repo):
    """Errors-by-category aggregation is derived from persisted events."""
    await _seed_retried_delivery(repo)
    servicer = _make_servicer(repo)

    resp = await servicer.GetDiagnosticFacetCounts(
        _facets(owner=7, facet="category"), FakeContext())
    by_cat = {c.facet: c.count for c in resp.counts}
    assert by_cat.get(C_TELEGRAM_API) == 2
    assert by_cat.get(C_DELIVERY) == 1


async def test_an_unwired_core_serves_empty_results_not_errors(repo):
    """A Core predating v33 degrades gracefully on every new RPC."""
    servicer = _make_servicer(None)  # no store attached

    resp = await servicer.QueryDiagnosticEvents(_query(owner=7), FakeContext())
    assert resp.total_returned == 0
    timeline = await servicer.GetMessageTimeline(
        _req(owner=7, source_peer_id="-100SRC", source_message_id=1), FakeContext())
    assert list(timeline.events) == []
    facets = await servicer.GetDiagnosticFacetCounts(_facets(owner=7), FakeContext())
    assert list(facets.counts) == []


# --------------------------------------------------------------------------- #
# Request builders — keep the wire types explicit so the test reads like the
# contract it pins.
# --------------------------------------------------------------------------- #

def _req(owner, source_peer_id="", source_message_id=0):
    from core.proto import autoforward_pb2 as pb
    return pb.MessageTimelineRequest(
        owner_user_id=owner, source_peer_id=source_peer_id,
        source_message_id=source_message_id)


def _query(owner, category="", stage="", severity="", reason="",
           source_message_id=0, limit=100):
    from core.proto import autoforward_pb2 as pb
    return pb.QueryDiagnosticEventsRequest(
        owner_user_id=owner, category=category, stage=stage,
        severity=severity, reason=reason, limit=limit,
        source_message_id=source_message_id)


def _facets(owner, facet="category", since=0.0):
    from core.proto import autoforward_pb2 as pb
    return pb.DiagnosticFacetCountsRequest(
        owner_user_id=owner, facet=facet, since_ts=since)
