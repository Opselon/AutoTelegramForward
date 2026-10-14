"""Real-PostgreSQL integration tests for audit events + transactional outbox.

Runs against the isolated stack's PostgreSQL (atf-hardening-postgres) using
the same connection path the application uses. Covers mission §5.3 / §13.3:
- business mutation + audit atomicity (commit and rollback)
- duplicate event_id handling
- publisher retry and outbox backlog recovery
- tenant-scoped queries
- retention pruning protecting security/audit categories
- query-fingerprint stability and driver error classification
"""

import os
import time
import uuid

import pytest

import asyncpg

from core.infrastructure.persistence.audit_events import (
    AuditEvent,
    FAILED,
    OUTCOME_UNCONFIRMED,
    SUCCEEDED,
)
from core.infrastructure.persistence.audit_outbox import AuditOutboxRepository
from core.infrastructure.persistence.db_observability import (
    classify_driver_error,
    query_fingerprint,
)

PG_URL = os.environ.get("ATF_TEST_PG_URL") or os.environ.get("ATF_DATABASE_URL") or \
    "postgresql://atf_hardening_user:atf_hardened_secure_dev_pw_1234@127.0.0.1:5433/atf_hardened_db"

# Real-PG tests are skipped when no reachable PG is available (CI, atf-core
# containers without network access to postgres). Run them from
# atf-hardened-core or with ATF_TEST_PG_URL pointing at a live instance.
requires_pg = pytest.mark.skipif(
    not os.environ.get("ATF_TEST_PG_URL") and not os.environ.get("ATF_DATABASE_URL"),
    reason="Real PostgreSQL unavailable — set ATF_TEST_PG_URL or ATF_DATABASE_URL",
)


@pytest.fixture()
def pg():
    async def _connect():
        return await asyncpg.connect(PG_URL)
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        conn = asyncio.run(_connect())
        yield conn
        conn.close()
        return
    conn = loop.run_until_complete(_connect())
    yield conn
    loop.run_until_complete(conn.close())


def test_fingerprint_stable_and_value_free():
    q1 = "SELECT * FROM forward_rules WHERE id = 'pg-proof-0001'"
    q2 = "SELECT * FROM forward_rules WHERE id = 'completely-different-id'"
    assert query_fingerprint(q1) == query_fingerprint(q2)
    assert "pg-proof" not in query_fingerprint(q1)
    assert len(query_fingerprint("UPDATE delivery_jobs SET status='X'")) <= 64


def test_driver_error_classification():
    class FakeViolation(Exception):
        sqlstate = "23505"

    assert "23505" in classify_driver_error(FakeViolation())
    assert classify_driver_error(ValueError("boom")).startswith("ValueError")


# --------------------------- real-PostgreSQL -------------------------------- #
@pytest.mark.asyncio
@requires_pg
async def test_schema_initialization_idempotent():
    conn = await asyncpg.connect(PG_URL)
    try:
        repo = AuditOutboxRepository(conn)
        await repo.initialize()
        await repo.initialize()  # second run must be a clean no-op
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN ('audit_events','audit_outbox')"
        )
        assert len(tables) == 2
    finally:
        await conn.close()


@pytest.mark.asyncio
@requires_pg
async def test_transactional_audit_commit_and_rollback():
    """The §5.1 contract: committed mutation keeps its audit row; a rolled
    back mutation produces no phantom audit record."""
    conn = await asyncpg.connect(PG_URL)
    repo = AuditOutboxRepository(conn)
    await repo.initialize()
    try:
        # COMMIT path
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO forward_rules (id, session_id, source_chat_id, target_chat_id,"
                " routing_type, forward_mode, is_active, created_at, updated_at, owner_user_id)"
                " VALUES ('txn-keep', 's', 'a', 'b', 'DIRECT', 'COPY', TRUE, 1, 1, 42)"
                " ON CONFLICT (id) DO NOTHING"
            )
            evt = AuditEvent(
                event_type="rule.create", service="core", outcome=SUCCEEDED,
                operation_id="op-keep", tenant_id="42", feature_id="FWD_RULE_CREATE",
            )
            await repo.record_transactional(evt, conn)
        row = await conn.fetchrow("SELECT event_type, tenant_id FROM audit_events WHERE event_id=$1", evt.event_id)
        assert row is not None, "committed mutation lost its audit record"
        assert row["tenant_id"] == "42"

        # ROLLBACK path
        evt2 = AuditEvent(
            event_type="rule.create", service="core", outcome=SUCCEEDED,
            operation_id="op-rollback", tenant_id="42",
        )
        try:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO forward_rules (id, session_id, source_chat_id, target_chat_id,"
                    " routing_type, forward_mode, is_active, created_at, updated_at, owner_user_id)"
                    " VALUES ('txn-rollback', 's', 'a', 'b', 'DIRECT', 'COPY', TRUE, 1, 1, 42)"
                )
                await repo.record_transactional(evt2, conn)
                raise RuntimeError("injected business failure")
        except RuntimeError:
            pass
        phantom = await conn.fetchval("SELECT COUNT(*) FROM audit_events WHERE event_id=$1", evt2.event_id)
        assert phantom == 0, "rolled-back mutation produced a phantom audit record"
        ghost = await conn.fetchval("SELECT COUNT(*) FROM forward_rules WHERE id='txn-rollback'")
        assert ghost == 0
    finally:
        await conn.execute("DELETE FROM forward_rules WHERE id IN ('txn-keep','txn-rollback')")
        await conn.close()


@pytest.mark.asyncio
@requires_pg
async def test_duplicate_event_id_persisted_once():
    conn = await asyncpg.connect(PG_URL)
    repo = AuditOutboxRepository(conn)
    await repo.initialize()
    try:
        evt = AuditEvent(event_type="dedup.check", service="core", outcome=SUCCEEDED, event_id="evt-dup-1")
        first = await repo.record(evt)
        # record() returns True but a re-insert with the same id must not
        # create a second row.
        await repo.record(AuditEvent(event_type="dedup.check", service="core", outcome=SUCCEEDED, event_id="evt-dup-1"))
        count = await conn.fetchval("SELECT COUNT(*) FROM audit_events WHERE event_id='evt-dup-1'")
        assert first is True
        assert count == 1
    finally:
        await conn.execute("DELETE FROM audit_events WHERE event_id='evt-dup-1'")
        await conn.close()


@pytest.mark.asyncio
@requires_pg
async def test_outbox_publication_lifecycle_and_backlog():
    conn = await asyncpg.connect(PG_URL)
    repo = AuditOutboxRepository(conn)
    await repo.initialize()
    try:
        evt = AuditEvent(event_type="outbox.pub", service="core", outcome=SUCCEEDED)
        await repo.record(evt)
        await repo.enqueue_for_publication(evt)
        backlog_before = await repo.outbox_backlog()
        assert backlog_before >= 1

        pending = await repo.pending_publications(limit=50)
        assert evt.event_id in pending

        # Publisher "crashes": mark failed, then recover.
        await repo.mark_publication_failed(evt.event_id, "logger unreachable", 1)
        assert await repo.outbox_backlog() >= 1

        # Publisher succeeds: ack clears the backlog entry.
        await repo.mark_published(evt.event_id)
        pending_after = await repo.pending_publications(limit=50)
        assert evt.event_id not in pending_after
    finally:
        await conn.execute("DELETE FROM audit_events WHERE event_id=$1", evt.event_id)
        await conn.execute("DELETE FROM audit_outbox WHERE event_id=$1", evt.event_id)
        await conn.close()


@pytest.mark.asyncio
@requires_pg
async def test_tenant_scoped_query_isolation():
    conn = await asyncpg.connect(PG_URL)
    repo = AuditOutboxRepository(conn)
    await repo.initialize()
    try:
        evt_a = AuditEvent(event_type="tenant.test", service="core", outcome=SUCCEEDED,
                           tenant_id="1001", correlation_id="corr-iso-a")
        evt_b = AuditEvent(event_type="tenant.test", service="core", outcome=SUCCEEDED,
                           tenant_id="2002", correlation_id="corr-iso-b")
        await repo.record(evt_a)
        await repo.record(evt_b)

        rows_a = await repo.query(tenant_id="1001", correlation_id="corr-iso-a")
        assert all(r["tenant_id"] == "1001" for r in rows_a)
        assert any(r["event_id"] == evt_a.event_id for r in rows_a)

        # Tenant A cannot discover Tenant B's activity by changing parameters.
        rows_b_as_a = await repo.query(tenant_id="1001", correlation_id="corr-iso-b")
        assert all(r["event_id"] != evt_b.event_id for r in rows_b_as_a)

        # Unscoped administrative query can see both (admin path only).
        rows_admin = await repo.query(correlation_id="corr-iso-a")
        assert any(r["event_id"] == evt_a.event_id for r in rows_admin)
    finally:
        await conn.execute("DELETE FROM audit_events WHERE event_id IN ($1, $2)",
                           evt_a.event_id, evt_b.event_id)
        await conn.close()


@pytest.mark.asyncio
@requires_pg
async def test_retention_prunes_diagnostics_but_protects_security_audit():
    conn = await asyncpg.connect(PG_URL)
    repo = AuditOutboxRepository(conn)
    await repo.initialize()
    try:
        old_ts = int(time.time()) - 400 * 86400  # 400 days old
        diag = AuditEvent(event_type="old.debug", service="core", outcome=SUCCEEDED,
                          event_category="feature_execution", ts_utc=old_ts)
        sec = AuditEvent(event_type="old.security", service="core", outcome=SUCCEEDED,
                         event_category="security", ts_utc=old_ts)
        await repo.record(diag)
        await repo.record(sec)

        await repo.prune_older_than(days=365)

        diag_left = await conn.fetchval("SELECT COUNT(*) FROM audit_events WHERE event_id=$1", diag.event_id)
        sec_left = await conn.fetchval("SELECT COUNT(*) FROM audit_events WHERE event_id=$1", sec.event_id)
        assert diag_left == 0, "diagnostic retention did not prune"
        assert sec_left == 1, "security/audit category was wrongly pruned"
    finally:
        await conn.execute("DELETE FROM audit_events WHERE event_id IN ($1, $2)",
                           diag.event_id, sec.event_id)
        await conn.close()
