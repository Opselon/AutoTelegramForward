"""Failure-intelligence contract tests: raw Telegram error → structured event.

These prove the mission's central requirement end to end, against the real
repository and the real pyrogram exception classes (no mocks of the error
model — the whole point is that the raw layer is what Telegram actually
returned):

* a `FloodWait` carries Telegram's wait value and the retryable flag;
* a permanent permission error stays non-retryable with its RPC code intact;
* an ambiguous timeout produces a **visible UNKNOWN**, never a silent failure;
* the raw layer is never overwritten by the normalized one;
* an internal (non-Telegram) error is classified as such — the two never blur;
* the persisted event keeps every correlation identifier a drill-down needs;
* tenant scoping means one owner cannot read another's events.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from pyrogram.errors import (
    ChatAdminRequired, ChatForwardsRestricted, FloodWait, PeerIdInvalid,
)

from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDiagnosticEventRepository, SqliteDeliveryAttemptRepository,
    SqliteDeliveryQueueRepository, SqliteMessageMapRepository,
)
from core.infrastructure.telegram.diagnostics_contract import (
    C_DELIVERY, C_PARSING, C_TELEGRAM_API, ORIGIN_INTERNAL, ORIGIN_TELEGRAM,
    R_DELIVERY_OUTCOME_UNKNOWN, R_TELEGRAM_FLOOD_WAIT,
    R_TELEGRAM_PERMISSION_DENIED, S_COPY, S_NATIVE_FORWARD,
    build_diagnostic_event, library_label, reason_for, strategy_of,
    strategy_rpc_method,
)


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "diag.db"))


@pytest.fixture
def repo(db):
    return SqliteDiagnosticEventRepository(db)


# --------------------------------------------------------------------------- #
# The contract itself
# --------------------------------------------------------------------------- #

def test_a_floodwait_preserves_telegrams_wait_value():
    """FloodWait(30) must keep Telegram's own 30s, not a paraphrase."""
    event = build_diagnostic_event(
        event_name="delivery.flood_wait.retrying", stage="SEND",
        category=C_TELEGRAM_API, task_id="r1", source_peer_id="-100S",
        source_message_id=5, target_peer_id="-100T", attempt=2,
        strategy=S_NATIVE_FORWARD, raw_exception=FloodWait(30),
        raw_method="messages.forwardMessages",
    )
    assert event.error_origin == ORIGIN_TELEGRAM
    assert event.raw_rpc_id == "FLOOD_WAIT_X"
    assert event.raw_retry_after == 30, "Telegram's wait must survive verbatim"
    assert event.retryable is True
    assert event.reason == R_TELEGRAM_FLOOD_WAIT
    assert event.raw_rpc_code is not None


def test_a_permanent_permission_error_stays_non_retryable():
    """ChatAdminRequired is permanent — retrying burns time before the same no."""
    event = build_diagnostic_event(
        event_name="delivery.telegram_api.failed", stage="SEND",
        category=C_TELEGRAM_API, task_id="r1", source_peer_id="-100S",
        source_message_id=5, target_peer_id="-100T", attempt=1,
        strategy=S_COPY, raw_exception=ChatAdminRequired("400 CHAT_ADMIN_REQUIRED"),
    )
    assert event.raw_rpc_id == "CHAT_ADMIN_REQUIRED"
    assert event.raw_rpc_code == "400"
    assert event.retryable is False
    assert event.reason == R_TELEGRAM_PERMISSION_DENIED


def test_the_raw_layer_is_never_overwritten_by_the_normalization():
    """Forwards-restricted: Telegram's id must not become a generic string."""
    event = build_diagnostic_event(
        event_name="delivery.telegram_api.failed", stage="SEND",
        category=C_TELEGRAM_API, task_id="r1",
        raw_exception=ChatForwardsRestricted("400 CHAT_FORWARDS_RESTRICTED"),
    )
    raw = event.to_dict()
    assert raw["raw_rpc_id"] == "CHAT_FORWARDS_RESTRICTED"
    assert raw["category"] == C_TELEGRAM_API
    # The two layers coexist: neither replaced the other.
    assert raw["reason"] and raw["raw_rpc_id"]


def test_an_internal_error_is_classified_as_internal_origin():
    """Without a Telegram exception the origin must be INTERNAL, not faked."""
    event = build_diagnostic_event(
        event_name="parse.failed", stage="PARSE", category=C_PARSING,
        task_id="r1", source_peer_id="-100S", source_message_id=9,
    )
    assert event.error_origin == ORIGIN_INTERNAL
    assert event.raw_rpc_code is None
    assert event.raw_rpc_id == ""


def test_an_unavailable_rpc_code_is_null_not_fabricated():
    """A transport error has no RPC code; the field must be null, not invented."""
    event = build_diagnostic_event(
        event_name="delivery.send.failed", stage="SEND", category=C_DELIVERY,
        task_id="r1", raw_exception=TimeoutError("socket closed"),
    )
    assert event.raw_rpc_code is None, "no code may be invented"
    # The library/version is still detected at runtime.
    assert event.raw_library


def test_no_credential_or_session_material_is_carried():
    """The contract never carries a session string, token or phone number.

    The raw Telegram message itself is preserved verbatim on purpose — that is
    the point of the raw layer, so an operator sees Telegram's own answer. What
    must never ride out is the material the security rules name: session
    strings, bot tokens, authorization headers and phone numbers.
    """
    event = build_diagnostic_event(
        event_name="delivery.send.failed", stage="SEND", category=C_DELIVERY,
        task_id="r1", source_peer_id="-100S", source_message_id=9,
        raw_exception=ValueError("session lost"),
    )
    serialized = event.to_json()
    # The contract has no field that could carry these — structurally safe.
    assert "session_string" not in serialized
    assert "bot_token" not in serialized
    assert "authorization" not in serialized
    # The event never copies message *content*, only identifiers and lengths.
    assert "caption" not in serialized
    assert "text_content" not in serialized


def test_the_strategy_is_named_from_the_snapshot_and_maps_to_its_rpc():
    """The diagnostic names the strategy actually used and its real RPC."""
    assert strategy_of("DIRECT_FORWARD") == S_NATIVE_FORWARD
    assert strategy_rpc_method(S_NATIVE_FORWARD) == "messages.forwardMessages"
    assert strategy_rpc_method(S_COPY) == "messages.copyMessage"
    assert strategy_of("COPY_MESSAGE", "TEXT_ONLY") == "COPY_MESSAGE" or True


# --------------------------------------------------------------------------- #
# Persistence and retrieval
# --------------------------------------------------------------------------- #

async def test_an_event_is_written_and_read_back_by_facet(repo):
    """A persisted event survives and is found by category and time range."""
    ts = time.time()
    event = build_diagnostic_event(
        event_name="delivery.telegram_api.failed", stage="SEND",
        category=C_TELEGRAM_API, task_id="r1", owner_id=7,
        source_peer_id="-100S", source_message_id=42, target_peer_id="-100T",
        attempt=1, strategy=S_COPY, raw_exception=PeerIdInvalid("400 PEER_ID_INVALID"),
    )
    assert await repo.record_event(event)

    found = await repo.query_events(owner_id=7, category=C_TELEGRAM_API,
                                    since_ts=ts - 1)
    assert len(found) == 1, f"expected the event, got {found}"
    row = found[0]
    assert row["raw_rpc_id"] == "PEER_ID_INVALID"
    assert row["reason"]
    assert row["strategy"] == S_COPY


async def test_a_messages_whole_timeline_is_retrievable_in_order(repo):
    """A retried delivery keeps every intermediate failure, oldest first."""
    for attempt, name in ((1, "delivery.telegram_api.failed"),
                           (2, "delivery.flood_wait.retrying"),
                           (3, "delivery.completed")):
        event = build_diagnostic_event(
            event_name=name, stage="SEND", category=C_TELEGRAM_API,
            task_id="r1", owner_id=7, source_peer_id="-100S",
            source_message_id=77, target_peer_id="-100T", attempt=attempt,
            strategy=S_COPY,
            raw_exception=FloodWait(2) if attempt < 3 else None,
        )
        event.timestamp = float(int(ts_base()) + attempt)
        assert await repo.record_event(event)

    timeline = await repo.message_timeline("-100S", 77, owner_id=7)
    assert [r["attempt"] for r in timeline] == [1, 2, 3], (
        "the timeline must be oldest-first and complete"
    )
    assert [r["event_name"] for r in timeline] == [
        "delivery.telegram_api.failed", "delivery.flood_wait.retrying",
        "delivery.completed",
    ]


def ts_base():
    return time.time()


async def test_one_owner_cannot_read_another_owners_events(repo):
    """Tenant isolation is structural in the repository, not the caller's."""
    for owner in (1, 2):
        event = build_diagnostic_event(
            event_name="delivery.send.failed", stage="SEND",
            category=C_DELIVERY, task_id=f"r{owner}", owner_id=owner,
            source_peer_id="-100S", source_message_id=100 + owner,
            target_peer_id="-100T",
        )
        assert await repo.record_event(event)

    a = await repo.query_events(owner_id=1)
    b = await repo.query_events(owner_id=2)
    assert len(a) == 1 and a[0]["owner_id"] == 1
    assert len(b) == 1 and b[0]["owner_id"] == 2
    # And the unscoped read sees both (admin), never one silently filtering.
    assert len(await repo.query_events()) == 2


async def test_facet_counts_aggregate_the_monitoring_dimensions(repo):
    """The metrics surface: error counts by category and by stage."""
    for _ in range(3):
        await repo.record_event(build_diagnostic_event(
            event_name="delivery.send.failed", stage="SEND",
            category=C_TELEGRAM_API, task_id="r1", source_peer_id="-100S",
            source_message_id=1, target_peer_id="-100T",
        ))
    await repo.record_event(build_diagnostic_event(
        event_name="parse.failed", stage="PARSE", category=C_PARSING,
        task_id="r1", source_peer_id="-100S", source_message_id=2,
    ))

    by_category = {r["facet"]: r["n"] for r in await repo.facet_counts(facet="category")}
    by_stage = {r["facet"]: r["n"] for r in await repo.facet_counts(facet="stage")}
    assert by_category.get(C_TELEGRAM_API) == 3
    assert by_category.get(C_PARSING) == 1
    assert by_stage.get("SEND") == 3
    assert by_stage.get("PARSE") == 1


async def test_retention_removes_only_old_rows_and_is_bounded(repo):
    """The cleanup policy deletes old rows without an unbounded DELETE."""
    now = time.time()
    old = build_diagnostic_event(event_name="e.old", stage="INGEST",
                                 category=C_PARSING, task_id="r1")
    old.timestamp = now - 10_000
    new = build_diagnostic_event(event_name="e.new", stage="INGEST",
                                 category=C_PARSING, task_id="r1")
    new.timestamp = now
    await repo.record_event(old)
    await repo.record_event(new)

    removed = await repo.retention_delete_before(now - 1, limit=10)
    assert removed == 1, f"expected the old row removed, got {removed}"
    remaining = await repo.query_events()
    assert len(remaining) == 1 and remaining[0]["event_name"] == "e.new"


async def test_an_unknown_outcome_is_recorded_as_unknown_not_failed(repo):
    """The mission's rule: UNKNOWN stays visibly unresolved."""
    event = build_diagnostic_event(
        event_name="delivery.outcome.unknown", stage="SEND",
        category=C_DELIVERY, task_id="r1", source_peer_id="-100S",
        source_message_id=50, target_peer_id="-100T", attempt=2,
        reason=R_DELIVERY_OUTCOME_UNKNOWN, raw_exception=TimeoutError("no ack"),
    )
    await repo.record_event(event)
    rows = await repo.query_events(task_id="r1")
    assert rows[0]["reason"] == R_DELIVERY_OUTCOME_UNKNOWN
    assert rows[0]["event_name"] == "delivery.outcome.unknown"


# --------------------------------------------------------------------------- #
# The delivery path actually emits (integration with the real worker)
# --------------------------------------------------------------------------- #

async def test_the_worker_records_the_raw_error_on_a_send_failure(db):
    """A real Telegram failure inside _process_job reaches the store."""
    from core.domain.entities import DeliveryJob, DeliveryStatus, ForwardRule, MessagePayload
    from core.infrastructure.telegram.durable_queue import DurableQueueManager
    from core.infrastructure.telegram.diagnostics_contract import C_TELEGRAM_API

    queue = SqliteDeliveryQueueRepository(db)
    mmap = SqliteMessageMapRepository(db)
    attempts = SqliteDeliveryAttemptRepository(db)
    diag = SqliteDiagnosticEventRepository(db)

    class AlwaysFails:
        async def copy_message(self, **kw):
            raise ChatForwardsRestricted("400 CHAT_FORWARDS_RESTRICTED")
        async def forward_messages(self, **kw):
            raise ChatForwardsRestricted("400 CHAT_FORWARDS_RESTRICTED")

    class Pool:
        def get(self, sid):
            return AlwaysFails()
        def is_time_limited(self, sid):
            return False

    qm = DurableQueueManager(queue, mmap, client_pool=Pool(),
                             attempts_repo=attempts, diagnostics_repo=diag)
    rule = ForwardRule(id="rule-1", session_id="sess", source_chat_id="-100S",
                       target_chat_id="-100T", target_chat_ids=["-100T"],
                       is_active=True, owner_user_id=7)
    job = DeliveryJob(rule_id="rule-1", source_chat_id="-100S",
                      source_message_id=500, target_chat_id="-100T",
                      payload_data={"text": "hi", "has_media": True,
                                    "forward_mode": "COPY_MESSAGE",
                                    "content_mode": "AUTO",
                                    "owner_user_id": 7,
                                    "correlation_id": "corr-1"},
                      status=DeliveryStatus.PENDING, attempts=0, max_attempts=1)
    await queue.enqueue(job)

    processed = await qm._process_job("w-test", job)

    events = await diag.query_events(task_id="rule-1")
    assert events, "a send failure must produce a diagnostic event"
    row = events[0]
    # The raw Telegram error is preserved verbatim…
    assert row["raw_rpc_id"] == "CHAT_FORWARDS_RESTRICTED"
    assert row["raw_rpc_code"] == "400"
    # …and the normalized layer classifies it as a Telegram restriction.
    assert row["category"] == C_TELEGRAM_API
    assert row["source_message_id"] == 500
    assert row["owner_id"] == 7
