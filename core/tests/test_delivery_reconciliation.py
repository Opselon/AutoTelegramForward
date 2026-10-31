"""Reconciliation: the pipeline must notice when a delivery falls through.

Each test creates the exact state the check exists for, then asserts the
reconciler flags it with the identifiers an operator needs. The checks run
against the real repositories (SQLite, in-memory); nothing is mocked, because
a reconciler that only works against a fake has no value in production.

The inverse contract is tested too: healthy state produces **no** findings.
A reconciler that cries wolf is worse than none.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from core.application.delivery_reconciler import DeliveryReconciler
from core.domain.entities import DeliveryStatus, ForwardRule, MessageMapping
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryAttemptRepository,
    SqliteDeliveryQueueRepository,
    SqliteForwardRuleRepository,
    SqliteIngestionGapRepository,
    SqliteMessageMapRepository,
)


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "recon.db"))


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
    """forward_rules carries an FK to sessions; give the rule its session."""
    db.execute(
        "INSERT OR IGNORE INTO sessions "
        "(id, phone_number, user_id, is_active, is_authorized, created_at, updated_at) "
        "VALUES ('sess', '100', '1', 1, 1, ?, ?)",
        (int(time.time()), int(time.time())),
    )


def make_rule(**overrides):
    base = dict(
        id="rule-1", session_id="sess", source_chat_id="-100S",
        target_chat_id="-100T", target_chat_ids=["-100T"], is_active=True,
    )
    base.update(overrides)
    return ForwardRule(**base)


def _job(db, *, status="PENDING", attempts=0, created_at=None, next_retry_at=0,
         lease_until=0, worker_id="", error_detail="", rule_id="rule-1",
         source_message_id=500, target_chat_id="-100T", job_id=None):
    """Insert one delivery job in an exact state."""
    jid = job_id or f"job-{rule_id}-{source_message_id}-{target_chat_id}"
    db.execute(
        "INSERT OR REPLACE INTO delivery_jobs "
        "(id, rule_id, source_chat_id, source_message_id, target_chat_id, status, "
        " payload_json, attempts, max_attempts, created_at, updated_at, next_retry_at, "
        " lease_until, worker_id, error_detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (jid, rule_id, "-100S", source_message_id, target_chat_id, status,
         json.dumps({"rule_id": rule_id, "text": "hi"}), attempts, 5,
         int(created_at or time.time()), int(time.time()), next_retry_at,
         lease_until, worker_id, error_detail),
    )
    return jid


def _mapping(db, *, rule_id="rule-1", source_message_id=500, target_chat_id="-100T",
             target_message_id=1001, status="SENT"):
    db.execute(
        "INSERT OR REPLACE INTO message_map "
        "(id, rule_id, source_chat_id, source_message_id, target_chat_id, "
        " target_message_id, delivery_status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (f"map-{rule_id}-{source_message_id}-{target_chat_id}", rule_id, "-100S",
         source_message_id, target_chat_id, target_message_id,
         status, int(time.time()), int(time.time())),
    )


def _reconciler(queue, mmap, rules, attempts=None, gaps=None, **kw):
    return DeliveryReconciler(queue, mmap, rules, attempts_repo=attempts, gap_repo=gaps, **kw)


# --------------------------------------------------------------------------- #
# Healthy state produces no findings
# --------------------------------------------------------------------------- #

async def test_a_healthy_pipeline_flags_nothing(db, queue, mmap, rules, attempts, gaps):
    """The wolf contract: clean state must yield an empty report."""
    await rules.add(make_rule())
    _job(db, status="SENT")
    _mapping(db)
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    assert r["totals"]["flagged"] == 0, r["checks"]
    assert all(v == [] for v in r["checks"].values())


async def test_the_totals_are_aggregated_correctly(db, queue, mmap, rules, attempts, gaps):
    """Every aggregate the health screen renders is computed from the jobs."""
    await rules.add(make_rule())
    _job(db, status="SENT", job_id="j1")
    _job(db, status="SENT", source_message_id=501, job_id="j2")
    _job(db, status="FAILED", source_message_id=502, job_id="j3")
    _job(db, status="RETRY_WAIT", source_message_id=503, job_id="j4")
    _job(db, status="DEAD", source_message_id=504, job_id="j5")

    m = await _reconciler(queue, mmap, rules, attempts, gaps).health_metrics()
    assert m["delivered"] == 2
    assert m["failed"] >= 1
    assert m["retrying"] == 1
    assert m["dead_letter"] == 1
    assert m["pending"] == 0
    assert m["success_rate"] is not None and 0 < m["success_rate"] < 1


# --------------------------------------------------------------------------- #
# Orphan mappings (E2E state mismatch)
# --------------------------------------------------------------------------- #

async def test_an_orphan_mapping_is_flagged(db, queue, mmap, rules, attempts, gaps):
    """A mapping with no delivery job is a state nothing else will resolve."""
    await rules.add(make_rule())
    _mapping(db)  # no job for it
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    orphans = r["checks"]["orphan_mappings"]
    assert len(orphans) == 1, orphans
    o = orphans[0]
    assert o["rule_id"] == "rule-1"
    assert o["source_message_id"] == 500
    assert o["destination_chat_id"] == "-100T"
    assert o["error_category"] == "orphan_mapping"


async def test_a_sent_job_with_its_mapping_is_not_an_orphan(db, queue, mmap, rules, attempts, gaps):
    """The join is on the full deliverable key, so a matching job clears it."""
    await rules.add(make_rule())
    _job(db, status="SENT")
    _mapping(db)
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    assert r["checks"]["orphan_mappings"] == []


# --------------------------------------------------------------------------- #
# Stuck pending
# --------------------------------------------------------------------------- #

async def test_a_job_stuck_past_its_retry_budget_is_flagged(db, queue, mmap, rules, attempts, gaps):
    """Pending forever with no terminal outcome is the silent class."""
    await rules.add(make_rule())
    # next_retry_at far in the past => long past the budget
    _job(db, status="RETRY_WAIT", next_retry_at=int(time.time()) - 100000)
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    stuck = r["checks"]["stuck_pending"]
    assert len(stuck) == 1, stuck
    assert stuck[0]["delivery_job_id"]
    assert stuck[0]["retryable"] is True


async def test_a_recently_created_pending_job_is_not_stuck(db, queue, mmap, rules, attempts, gaps):
    """A fresh job is just queued, not abandoned."""
    await rules.add(make_rule())
    _job(db, status="PENDING")
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    assert r["checks"]["stuck_pending"] == []


# --------------------------------------------------------------------------- #
# Expired claims (stranded workers)
# --------------------------------------------------------------------------- #

async def test_a_claim_past_its_lease_is_flagged(db, queue, mmap, rules, attempts, gaps):
    """A CLAIMED job whose lease is long expired is a stranded worker."""
    await rules.add(make_rule())
    _job(db, status="CLAIMED", lease_until=int(time.time()) - 3600, worker_id="w-dead")
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    expired = r["checks"]["expired_claims"]
    assert len(expired) == 1, expired
    assert expired[0]["worker_id"] == "w-dead"
    assert expired[0]["error_category"] == "expired_lease"


async def test_a_live_claim_is_not_flagged(db, queue, mmap, rules, attempts, gaps):
    """A lease still inside its window means the worker is simply running."""
    await rules.add(make_rule())
    _job(db, status="CLAIMED", lease_until=int(time.time()) + 30, worker_id="w-live")
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    assert r["checks"]["expired_claims"] == []


# --------------------------------------------------------------------------- #
# UNKNOWN deliveries
# --------------------------------------------------------------------------- #

async def test_an_unresolved_unknown_delivery_is_flagged(db, queue, mmap, rules, attempts, gaps):
    """A timed-out send left in UNKNOWN has no recovery path of its own."""
    await rules.add(make_rule())
    _job(db, status="UNKNOWN", error_detail="timed out")
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    unknown = r["checks"]["unknown_deliveries"]
    assert len(unknown) == 1, unknown
    assert "timed out" in unknown[0]["error_detail"], unknown[0]["error_detail"]


# --------------------------------------------------------------------------- #
# Repeated failures per destination / per source
# --------------------------------------------------------------------------- #

async def test_a_destination_failing_repeatedly_is_flagged(db, queue, mmap, rules, attempts, gaps):
    """A 50%+ per-destination failure rate is the actionable signal."""
    await rules.add(make_rule())
    _job(db, status="SENT", job_id="j1", target_chat_id="-100T")
    _job(db, status="FAILED", source_message_id=501, job_id="j2", target_chat_id="-100T")
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    repeated = r["checks"]["repeated_failures"]
    kinds = {f["error_category"] for f in repeated}
    assert "repeated_destination_failure" in kinds, repeated
    dest = [f for f in repeated if f["error_category"] == "repeated_destination_failure"]
    assert dest[0]["destination_chat_id"] == "-100T"


async def test_a_healthy_destination_is_not_flagged(db, queue, mmap, rules, attempts, gaps):
    """One failure in many deliveries is normal, not a repeated failure."""
    await rules.add(make_rule())
    for i in range(10):
        _job(db, status="SENT", source_message_id=500 + i, job_id=f"j{i}")
    _job(db, status="FAILED", source_message_id=600, job_id="jbad")
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    repeated = [f for f in r["checks"]["repeated_failures"]
                if f["error_category"] == "repeated_destination_failure"]
    assert repeated == [], repeated


async def test_a_source_failing_repeatedly_is_flagged(db, queue, mmap, rules, attempts, gaps):
    """The per-source view catches a broken source the totals would hide."""
    await rules.add(make_rule())
    _job(db, status="SENT", job_id="j1")
    _job(db, status="FAILED", source_message_id=501, job_id="j2")
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    kinds = {f["error_category"] for f in r["checks"]["repeated_failures"]}
    assert "repeated_source_failure" in kinds, r["checks"]["repeated_failures"]


# --------------------------------------------------------------------------- #
# Ingestion gaps that exhausted their budget
# --------------------------------------------------------------------------- #

async def test_an_exhausted_ingestion_gap_is_flagged(db, queue, mmap, rules, attempts, gaps):
    """A gap that used its whole budget without closing is actionable."""
    await rules.add(make_rule())
    db.execute(
        "INSERT OR REPLACE INTO ingestion_gaps "
        "(id, session_id, source_chat_id, source_message_id, state, attempts, last_error, "
        " created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("gap-1", "sess", "-100S", 100, "PENDING", 5, "kept failing", int(time.time()), int(time.time())),
    )
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    open_gaps = r["checks"]["open_gaps"]
    assert len(open_gaps) == 1, open_gaps
    assert open_gaps[0]["source_message_id"] == 100
    assert "unrecovered after 5" in open_gaps[0]["error_detail"]


async def test_a_gap_still_within_its_budget_is_not_flagged(db, queue, mmap, rules, attempts, gaps):
    """A gap mid-recovery is work in progress, not an alarm."""
    await rules.add(make_rule())
    db.execute(
        "INSERT OR REPLACE INTO ingestion_gaps "
        "(id, session_id, source_chat_id, source_message_id, state, attempts, last_error, "
        " created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("gap-2", "sess", "-100S", 100, "PENDING", 2, "", int(time.time()), int(time.time())),
    )
    r = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile()
    assert r["checks"]["open_gaps"] == []


# --------------------------------------------------------------------------- #
# Tenancy: one owner never sees another's deliveries
# --------------------------------------------------------------------------- #

async def test_an_owner_only_sees_their_own_findings(db, queue, mmap, rules, attempts, gaps):
    """The report is owner-scoped: tenant A's stray job is invisible to B."""
    await rules.add(make_rule(id="rule-a", owner_user_id=1))
    await rules.add(make_rule(id="rule-b", owner_user_id=2, target_chat_id="-100T2"))
    # Tenant B has an orphan mapping; tenant A is clean.
    _job(db, status="SENT", rule_id="rule-a")
    _mapping(db, rule_id="rule-a")
    _mapping(db, rule_id="rule-b", target_chat_id="-100T2")

    r_a = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile(owner_user_id=1)
    r_b = await _reconciler(queue, mmap, rules, attempts, gaps).reconcile(owner_user_id=2)
    assert r_a["totals"]["flagged"] == 0, r_a["checks"]
    assert r_b["totals"]["flagged"] >= 1, r_b["checks"]
    assert r_b["checks"]["orphan_mappings"], "tenant B must see its own orphan"
    assert all(f["rule_id"] == "rule-b" for f in r_b["checks"]["orphan_mappings"])


# --------------------------------------------------------------------------- #
# Failure isolation: one broken check cannot hide the others
# --------------------------------------------------------------------------- #

async def test_one_failing_check_does_not_blank_the_report(db, queue, mmap, rules, attempts, gaps):
    """A check that raises is reported, not swallowed with the rest dropped."""
    await rules.add(make_rule())
    _job(db, status="UNKNOWN")

    rec = _reconciler(queue, mmap, rules, attempts, gaps)

    async def broken(_owner=None):
        raise RuntimeError("simulated check failure")

    rec._check_repeated_failures = broken
    r = await rec.reconcile()
    # The broken check contributed nothing, but the UNKNOWN one still shows.
    assert r["checks"]["unknown_deliveries"], "the other checks must still run"
    assert r["checks"]["repeated_failures"] == []
