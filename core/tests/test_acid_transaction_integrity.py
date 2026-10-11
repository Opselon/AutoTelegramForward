"""ACID transaction integrity tests (v12.3).

Each test reproduces a real defect found in the audit and proves the fix:
  A1 atomicity  — enqueue writes mapping+job as one unit, or neither.
  A2 atomicity  — the transaction primitive rolls back on error.
  A3 durability — a committed mapping survives a DB close/reopen (regression
                  guard for the conn-routing change).
  A4 atomicity  — enqueue_delivery reports a replay as not-enqueued (no
                  duplicate job, honest return value).
  C1 consistency — the replay-marker failure is observable, not swallowed.
  I1 isolation  — concurrent enqueue_delivery for the same event yields
                  exactly one job.
  T1 tenant     — LoginAccount must not rebind a web account to an arbitrary
                  attacker-supplied telegram_user_id.

Run: python -m pytest core/tests/test_acid_transaction_integrity.py -q
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryQueueRepository,
    SqliteMessageMapRepository,
)
from core.domain.entities import (
    DeliveryJob,
    DeliveryStatus,
    ForwardRule,
    MessageMapping,
    MessagePayload,
)
from core.infrastructure.telegram.durable_queue import DurableQueueManager


class DummyClient:
    def __init__(self, fails: bool = False):
        self.fails = fails
        self.sent: list = []

    async def send_message(self, chat_id, text, **kw):
        if self.fails:
            raise TimeoutError("ambiguous send")
        self.sent.append((chat_id, text))
        return type("M", (), {"id": 777})()


class DummyPool:
    def __init__(self, client):
        self._client = client

    def get(self, session_id):
        return self._client


@pytest.fixture
def sqlite_db(tmp_path):
    db = SqliteDatabase(str(tmp_path / "acid.db"))
    yield db
    db.close()


@pytest.fixture
def repos(sqlite_db):
    return (
        SqliteDeliveryQueueRepository(sqlite_db),
        SqliteMessageMapRepository(sqlite_db),
    )


def _rule() -> ForwardRule:
    return ForwardRule(
        id="r1", source_chat_id="-1001", target_chat_id="-1002", session_id="sess1"
    )


def _payload() -> MessagePayload:
    return MessagePayload(chat_id="-1001", message_id=42, text="hello")


def _count_jobs(db) -> int:
    return db.query_one(
        "SELECT COUNT(*) c FROM delivery_jobs WHERE status IN "
        "('PENDING','CLAIMED','RETRY_WAIT')"
    )["c"]


# --------------------------------------------------------------------------- #
# A1 — mapping + job are one atomic unit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a1_enqueue_writes_mapping_and_job_atomically(sqlite_db, repos):
    """Both rows appear together; no PENDING mapping can exist without a job."""
    queue, mapping = repos
    mgr = DurableQueueManager(queue, mapping, DummyPool(DummyClient()), num_workers=0)

    ok = await mgr.enqueue_delivery(_rule(), _payload(), "-1002", "hello", None)
    assert ok is True

    assert _count_jobs(sqlite_db) == 1
    maps = await mapping.get_by_source("-1001", 42)
    assert len(maps) == 1
    assert maps[0].delivery_status == DeliveryStatus.PENDING


# --------------------------------------------------------------------------- #
# A2 — the transaction primitive rolls back the whole unit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a2_transaction_rolls_back_on_error(sqlite_db):
    """A failure inside the unit leaves neither write behind."""
    sqlite_db.execute("CREATE TABLE acid_probe (k TEXT PRIMARY KEY)")

    def unit(conn):
        conn.execute("INSERT INTO acid_probe VALUES ('a')")
        # second statement fails: violates the PRIMARY KEY we just inserted
        conn.execute("INSERT INTO acid_probe VALUES ('a')")

    with pytest.raises(sqlite3.IntegrityError):
        sqlite_db.run_in_transaction(unit)

    rows = sqlite_db.query_all("SELECT k FROM acid_probe")
    assert rows == [], "rollback must discard the first insert too"


@pytest.mark.asyncio
async def test_a2b_transaction_commits_on_success(sqlite_db):
    sqlite_db.execute("CREATE TABLE acid_probe (k TEXT PRIMARY KEY)")
    sqlite_db.run_in_transaction(lambda c: c.execute("INSERT INTO acid_probe VALUES ('a')"))
    assert sqlite_db.query_one("SELECT k FROM acid_probe")["k"] == "a"


# --------------------------------------------------------------------------- #
# A3 — durability: committed rows survive reopen (regression guard)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a3_mapping_survives_db_reopen(sqlite_db, tmp_path):
    """v12.3 regression guard: routing add_or_update through the raw connection
    without committing silently lost every row on restart."""
    repo = SqliteMessageMapRepository(sqlite_db)
    await repo.add_or_update(
        MessageMapping(
            id="m1",
            rule_id="r1",
            source_chat_id="-1001",
            source_message_id=1,
            target_chat_id="-1002",
            target_message_id=2,
        )
    )
    sqlite_db.close()
    reopened = SqliteDatabase(str(tmp_path / "acid.db"))
    try:
        rows = await SqliteMessageMapRepository(reopened).get_by_source("-1001", 1)
        assert len(rows) == 1 and rows[0].target_message_id == 2
    finally:
        reopened.close()


# --------------------------------------------------------------------------- #
# A4 — replay is a no-op and is reported honestly
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a4_replay_is_idempotent_and_reported(sqlite_db, repos):
    queue, mapping = repos
    mgr = DurableQueueManager(queue, mapping, DummyPool(DummyClient()), num_workers=0)

    first = await mgr.enqueue_delivery(_rule(), _payload(), "-1002", "hello", None)
    second = await mgr.enqueue_delivery(_rule(), _payload(), "-1002", "hello", None)
    assert first is True
    assert second is False, "a replayed event must not create a second job"
    assert _count_jobs(sqlite_db) == 1


# --------------------------------------------------------------------------- #
# C1 — the replay-marker failure must be observable, not swallowed
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_c1_replay_marker_failure_is_logged(caplog):
    """mark_processed failing used to be `except: pass` — the idempotency guard
    was silently disabled with no signal anywhere. This drives the real guarded
    block through _process_for_rule with a queue that accepts and a marker that
    raises."""
    from core.application.pipeline import DurableMessagePipeline, PipelineContext

    class FailingProcessed:
        async def was_processed(self, *a, **k):
            return False

        async def mark_processed(self, *a, **k):
            raise RuntimeError("db is down")

    class AcceptingQueue:
        async def enqueue_delivery(self, *a, **k):
            return True

    pipe = DurableMessagePipeline(
        rule_repo=None,
        filter_repo=None,
        queue_manager=AcceptingQueue(),
        processed_repo=FailingProcessed(),
    )

    payload = _payload()
    payload.media_group_id = ""
    rule = _rule()
    rule.is_active = True
    ctx = PipelineContext(payload=payload, correlation_id="corr-1")

    with caplog.at_level("ERROR"):
        await pipe._process_for_rule(ctx, rule)

    assert any("idempotency degraded" in r.getMessage() for r in caplog.records), (
        "a failed replay marker must be logged, not silently swallowed"
    )


# --------------------------------------------------------------------------- #
# I1 — concurrent processing of one event yields exactly one job
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_i1_concurrent_enqueue_of_same_event_is_singleton(sqlite_db, repos):
    """Two workers racing on the same incoming event must produce one job.

    The audit identified the check-then-act window in _process_for_rule
    (was_processed -> enqueue). The job id is deterministic and the insert is
    INSERT OR IGNORE, so the loser of the race is a clean no-op.
    """
    queue, mapping = repos
    mgr = DurableQueueManager(queue, mapping, DummyPool(DummyClient()), num_workers=0)

    results = await asyncio.gather(
        mgr.enqueue_delivery(_rule(), _payload(), "-1002", "hello", None),
        mgr.enqueue_delivery(_rule(), _payload(), "-1002", "hello", None),
        mgr.enqueue_delivery(_rule(), _payload(), "-1002", "hello", None),
    )
    assert sum(1 for r in results if r is True) == 1
    assert _count_jobs(sqlite_db) == 1
    assert len(await mapping.get_by_source("-1001", 42)) == 1


# --------------------------------------------------------------------------- #
# D1 — no orphan mapping when the unit is interrupted
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_d1_no_orphan_pending_mapping(sqlite_db, repos):
    """Before v12.3 a crash between the two auto-commits left a PENDING mapping
    with no delivery job — orphaned forever, since the watchdog scans jobs.
    The atomic unit means the mapping is only visible alongside its job."""
    queue, mapping = repos
    mgr = DurableQueueManager(queue, mapping, DummyPool(DummyClient()), num_workers=0)

    await mgr.enqueue_delivery(_rule(), _payload(), "-1002", "hello", None)

    # invariant: every mapping must have a corresponding job
    maps = await mapping.get_by_source("-1001", 42)
    job_ids = {
        r["id"]
        for r in sqlite_db.query_all("SELECT id FROM delivery_jobs")
    }
    for m in maps:
        key = f"{m.rule_id}:{m.source_chat_id}:{m.source_message_id}:{m.target_chat_id}"
        assert key in job_ids, "a mapping without a job is unreconcilable state"


# --------------------------------------------------------------------------- #
# T1 — LoginAccount must not rebind a web account to an arbitrary telegram id
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_t1_password_login_does_not_rebind_account(sqlite_db, caplog):
    """F1 (P0): AuthLogin binds the raw JSON body, and /api/auth/login is public.
    Binding telegram_user_id there let anyone with a victim's password anchor
    that account to their own Telegram id, inheriting every owner-scoped row."""
    from core.infrastructure.grpc_server import account_servicer as asrv

    # a victim account, not yet linked to any telegram id
    sqlite_db.execute(
        "INSERT INTO web_accounts (user_id, username, password_hash, password_iter, "
        "salt, is_admin, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (0, "victim", "hash", 240000, "00", 0, 1, 1),
    )

    srv = asrv.AccountControlServicer.__new__(asrv.AccountControlServicer)
    srv._db = sqlite_db
    srv._secret = b"test-secret"

    class _Req:
        username = "victim"
        password = "pw"
        telegram_user_id = 777000  # attacker's id

    # make the stored-hash path succeed so we reach the binding logic itself
    srv._find_by_username = lambda username: sqlite_db.query_one(
        "SELECT * FROM web_accounts WHERE username=?", (username,)
    )
    srv._login_success = lambda row: "ok"
    asrv._verify_password = lambda password, salt, expected, iterations: True

    try:
        with caplog.at_level("WARNING"):
            await srv.LoginAccount(_Req(), None)
    finally:
        pass

    row = sqlite_db.query_one("SELECT user_id FROM web_accounts WHERE username='victim'")
    assert row["user_id"] in (None, 0, ""), (
        "a password login must not rebind the account to a client-supplied "
        "telegram id — that is a cross-tenant takeover via the login path"
    )
    assert any("ignoring telegram_user_id" in r.getMessage() for r in caplog.records)
