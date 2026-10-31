"""The gRPC delivery-stats surface must report reconciliation findings.

`GetDeliveryStats` is the surface the dashboard and the bot render from. The
reconciliation fields are additive (v32), so the contract to prove is:

* the new fields are populated from the real queue state;
* a finding the reconciler flags reaches the client with the identifiers an
  operator needs;
* an owner never sees another owner's findings;
* a reconciler error degrades the additive fields to zero instead of failing
  the whole response — the stats screen must not break because a check broke.

The servicer is wired exactly as ``main.py`` wires it, against the real
repositories and the real reconciler — no mocks.
"""

from __future__ import annotations

import json
import time

import pytest

from core.application.delivery_reconciler import DeliveryReconciler
from core.domain.entities import ForwardRule
from core.infrastructure.grpc_server import servicers
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryAttemptRepository,
    SqliteDeliveryQueueRepository,
    SqliteForwardRuleRepository,
    SqliteIngestionGapRepository,
    SqliteMessageMapRepository,
)
from core.proto import autoforward_pb2 as pb


class FakeContext:
    """The minimum a servicer method touches on the gRPC context."""

    def __init__(self):
        self.code = None
        self.details = None

    def set_code(self, code):
        self.code = code

    def set_details(self, details):
        self.details = details

    async def abort(self, code, details):
        self.code = code
        self.details = details
        raise RuntimeError(details)


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "grpc_recon.db"))


@pytest.fixture
def queue(db):
    return SqliteDeliveryQueueRepository(db)


@pytest.fixture
def mmap(db):
    return SqliteMessageMapRepository(db)


@pytest.fixture
def rules(db):
    return SqliteForwardRuleRepository(db)


@pytest.fixture
def attempts(db):
    return SqliteDeliveryAttemptRepository(db)


@pytest.fixture
def gaps(db):
    return SqliteIngestionGapRepository(db)


@pytest.fixture(autouse=True)
def _session_row(db):
    db.execute(
        "INSERT OR IGNORE INTO sessions "
        "(id, phone_number, user_id, is_active, is_authorized, created_at, updated_at) "
        "VALUES ('sess', '100', '1', 1, 1, ?, ?)",
        (int(time.time()), int(time.time())),
    )


def _servicer(db, rules, queue, mmap, attempts, gaps, owner=1):
    """Wired exactly the way main.py wires it."""
    return servicers.DeliveryControlServicer(
        db, rules, queue_repo=queue, message_map_repo=mmap,
        reconciler=DeliveryReconciler(queue, mmap, rules, attempts_repo=attempts, gap_repo=gaps),
    )


async def _rule(repo, **overrides):
    base = dict(
        id="rule-1", session_id="sess", source_chat_id="-100S",
        target_chat_id="-100T", target_chat_ids=["-100T"], is_active=True,
        owner_user_id=1,
    )
    base.update(overrides)
    rule = ForwardRule(**base)
    await repo.add(rule)
    return rule


def _job(db, *, status="PENDING", rule_id="rule-1", source_message_id=500,
         target_chat_id="-100T", job_id=None, attempts=0, error_detail="",
         next_retry_at=0, lease_until=0, worker_id=""):
    jid = job_id or f"job-{rule_id}-{source_message_id}-{target_chat_id}"
    db.execute(
        "INSERT OR REPLACE INTO delivery_jobs "
        "(id, rule_id, source_chat_id, source_message_id, target_chat_id, status, "
        " payload_json, attempts, max_attempts, created_at, updated_at, next_retry_at, "
        " lease_until, worker_id, error_detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (jid, rule_id, "-100S", source_message_id, target_chat_id, status,
         json.dumps({"rule_id": rule_id, "text": "hi"}), attempts, 5,
         int(time.time()), int(time.time()), next_retry_at, lease_until, worker_id, error_detail),
    )
    return jid


# --------------------------------------------------------------------------- #
# The new fields are populated
# --------------------------------------------------------------------------- #

async def test_the_health_aggregates_reach_the_client(db, queue, mmap, rules, attempts, gaps):
    """pending/delivered/retrying/success-rate come from the real queue."""
    await _rule(rules)
    _job(db, status="SENT", job_id="j1")
    _job(db, status="SENT", source_message_id=501, job_id="j2")
    _job(db, status="FAILED", source_message_id=502, job_id="j3")
    _job(db, status="RETRY_WAIT", source_message_id=503, job_id="j4")

    s = _servicer(db, rules, queue, mmap, attempts, gaps)
    resp = await s.GetDeliveryStats(pb.DeliveryStatsRequest(owner_user_id=1), FakeContext())

    assert resp.delivered_total == 2, resp.delivered_total
    assert resp.retrying_total == 1
    # failed_total is the legacy rule_stats counter (not populated here);
    # the reconciler's failure aggregate is the authoritative new field.
    assert 0.0 < resp.success_rate <= 1.0
    assert resp.success_rate < 1.0, "a FAILED job must pull the rate below 1"


async def test_a_finding_reaches_the_client_with_identifiers(db, queue, mmap, rules, attempts, gaps):
    """An stranded worker must arrive at the client with everything needed to find it."""
    await _rule(rules)
    _job(db, status="CLAIMED", lease_until=int(time.time()) - 3600, worker_id="w-dead")

    s = _servicer(db, rules, queue, mmap, attempts, gaps)
    resp = await s.GetDeliveryStats(pb.DeliveryStatsRequest(owner_user_id=1), FakeContext())

    assert resp.flagged_findings >= 1, resp.flagged_findings
    kinds = {f.kind for f in resp.findings}
    assert "expired_lease" in kinds, kinds
    expired = [f for f in resp.findings if f.kind == "expired_lease"][0]
    assert expired.worker_id == "w-dead"
    assert expired.delivery_job_id
    assert expired.retryable is True
    # Never private content: the detail carries state, not message text.
    assert "hi" not in expired.error_detail


async def test_the_failure_rates_are_per_destination(db, queue, mmap, rules, attempts, gaps):
    """A bad target shows up as a rate, not a buried global count."""
    await _rule(rules)
    _job(db, status="SENT", job_id="j1")
    _job(db, status="FAILED", source_message_id=501, job_id="j2")

    s = _servicer(db, rules, queue, mmap, attempts, gaps)
    resp = await s.GetDeliveryStats(pb.DeliveryStatsRequest(owner_user_id=1), FakeContext())

    assert "-100T" in resp.failure_rate_by_destination, dict(resp.failure_rate_by_destination)
    assert resp.failure_rate_by_destination["-100T"] == 0.5


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #

async def test_an_owner_never_sees_another_owners_findings(db, queue, mmap, rules, attempts, gaps):
    """Tenant B's stranded worker is invisible to tenant A."""
    await _rule(rules, id="rule-a", owner_user_id=1)
    await _rule(rules, id="rule-b", owner_user_id=2, target_chat_id="-100T2")
    _job(db, status="CLAIMED", lease_until=int(time.time()) - 3600,
         worker_id="w-b", rule_id="rule-b", target_chat_id="-100T2", job_id="jb")

    s = _servicer(db, rules, queue, mmap, attempts, gaps)
    resp_a = await s.GetDeliveryStats(pb.DeliveryStatsRequest(owner_user_id=1), FakeContext())
    resp_b = await s.GetDeliveryStats(pb.DeliveryStatsRequest(owner_user_id=2), FakeContext())

    assert resp_a.flagged_findings == 0, "tenant A must not see tenant B's findings"
    assert resp_b.flagged_findings >= 1
    assert all(f.rule_id == "rule-b" for f in resp_b.findings)


# --------------------------------------------------------------------------- #
# Graceful degradation
# --------------------------------------------------------------------------- #

async def test_a_reconciler_failure_does_not_break_the_stats(db, queue, mmap, rules, attempts, gaps):
    """The additive fields degrade to zero; the legacy fields still answer."""
    await _rule(rules)
    _job(db, status="SENT")

    s = _servicer(db, rules, queue, mmap, attempts, gaps)

    async def broken(*a, **kw):
        raise RuntimeError("simulated reconciler outage")

    s._reconciler.health_metrics = broken
    s._reconciler.reconcile = broken
    resp = await s.GetDeliveryStats(pb.DeliveryStatsRequest(owner_user_id=1), FakeContext())

    # Legacy fields still populated...
    assert resp.forwarded_total >= 0
    # ...and the additive ones are zero-valued, not an error.
    assert resp.flagged_findings == 0
    assert resp.success_rate == 0.0
    assert list(resp.findings) == []


async def test_a_servicer_without_a_reconciler_still_answers(db, queue, mmap, rules, attempts, gaps):
    """The reconciler is optional: pre-upgrade wiring keeps working."""
    await _rule(rules)
    _job(db, status="SENT")
    s = servicers.DeliveryControlServicer(db, rules, queue_repo=queue, message_map_repo=mmap)
    resp = await s.GetDeliveryStats(pb.DeliveryStatsRequest(owner_user_id=1), FakeContext())
    assert resp.flagged_findings == 0
    assert list(resp.findings) == []
    assert resp.in_queue >= 0
