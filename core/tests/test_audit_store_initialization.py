"""Audit-store initialization regression tests (real PostgreSQL, opt-in).

``core/main.py`` constructs ``AuditOutboxRepository`` on the PostgreSQL
boundary but never calls ``initialize()``, so the ``audit_events`` and
``audit_outbox`` tables did not exist. Every audit write then raised
``UndefinedTableError`` — and because the worker's audit helper deliberately
swallows exceptions ("audit must not kill a task"), a PostgreSQL deployment
silently lost every audit record while the code path looked correct in review.
These tests pin both halves of the fix:

* ``_resolve_pg_pool`` initializes the audit store on **both** the async and
  the sync bootstrap paths, after the pool is resolved (init before the rebind
  would run against the un-awaited pool coroutine and no-op).
* ``AuditOutboxRepository.initialize`` uses the dollar-quote-aware splitter,
  so a procedural ``DO $$ ... $$`` block in AUDIT_DDL cannot be fragmented the
  way the bootstrap's own dead-letter/``web_login_codes`` statements once were.

Set ``ATF_TEST_POSTGRES_URL`` to run the real-server tests.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from pathlib import Path

import pytest

from core.infrastructure.persistence import audit_outbox as ao

DSN = os.environ.get("ATF_TEST_POSTGRES_URL") or os.environ.get("ATF_DATABASE_URL") or ""

requires_pg = pytest.mark.skipif(
    not DSN,
    reason="Real PostgreSQL unavailable — set ATF_TEST_POSTGRES_URL",
)


def _main_py() -> str:
    return Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The startup path initializes the audit store
# --------------------------------------------------------------------------- #

class TestAuditStoreInitializationWiring:
    def test_resolve_pg_pool_initializes_the_audit_store(self):
        """Both bootstrap paths must create the audit tables."""
        src = _main_py()
        # The async path: after the pool coroutine is awaited and rebound.
        assert "audit_repo.initialize()" in src, (
            "_resolve_pg_pool no longer initializes the audit store — the "
            "tables vanish and every audit write raises UndefinedTableError"
        )
        # The guard must run on the sync path too (early return when the pool
        # is already a pool, not a coroutine).
        early = src.split("resolved = await pool", 1)
        assert len(early) == 2, "the async bootstrap path is missing"
        sync_part = early[0]
        assert "audit_repo.initialize()" in sync_part, (
            "the sync bootstrap path (pool already resolved) skips audit "
            "schema initialization"
        )

    def test_initialize_runs_after_the_pool_is_rebound(self):
        src = _main_py()
        rebind = src.index("audit_repo._db = resolved")
        init = src.index("audit_repo.initialize()", rebind)
        assert init > rebind, (
            "initialize() must run after the pool is rebound — before it, the "
            "repository still holds the un-awaited pool coroutine"
        )

    def test_initialize_failure_does_not_block_startup(self):
        """A broken audit schema is a warning, not a dead core."""
        src = _main_py()
        assert "except Exception as exc:" in src
        assert "Audit store schema initialization failed" in src, (
            "an initialization failure must be visible in the log, not silent"
        )


# --------------------------------------------------------------------------- #
# The splitter the store now uses
# --------------------------------------------------------------------------- #

class TestAuditDdlSplitting:
    """AUDIT_DDL is split with the shared dollar-quote-aware splitter."""

    def test_initialize_uses_the_shared_splitter(self):
        src = inspect.getsource(ao.AuditOutboxRepository.initialize)
        assert "_split_sql_script" in src, (
            "initialize() fell back to a naive split on ';' — a semicolon "
            "inside a comment or a DO block would ship fragments to the server"
        )
        assert '.split(";")' not in src

    def test_every_statement_is_whole_ddl(self):
        from core.infrastructure.persistence.postgres_repositories import _split_sql_script

        statements = _split_sql_script(ao.AUDIT_DDL)
        assert statements, "AUDIT_DDL produced no statements"
        # The two CREATE TABLEs survive as whole statements.
        joined = "\n".join(statements)
        assert "CREATE TABLE IF NOT EXISTS audit_events" in joined
        assert "CREATE TABLE IF NOT EXISTS audit_outbox" in joined
        # No fragment starts mid-DDL (the naive-split signature).
        for stmt in statements:
            assert not stmt.lstrip().startswith("event_id"), f"fragment: {stmt[:60]!r}"
            assert not stmt.lstrip().startswith("published_at"), f"fragment: {stmt[:60]!r}"


# --------------------------------------------------------------------------- #
# Real PostgreSQL: initialize then write and read back
# --------------------------------------------------------------------------- #

@requires_pg
class TestAuditStoreOnRealPostgres:
    def test_initialize_creates_the_tables_and_a_write_round_trips(self):
        from core.infrastructure.persistence.audit_events import AuditEvent, SUCCEEDED

        async def body():
            import asyncpg
            pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
            try:
                repo = ao.AuditOutboxRepository(pool)
                # The path main.py takes: initialize, then write. Idempotent.
                await repo.initialize()
                await repo.initialize()
                async with pool.acquire() as con:
                    tables = {r["tablename"] for r in await con.fetch(
                        "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                        " AND tablename LIKE 'audit%'")}
                    assert {"audit_events", "audit_outbox"} <= tables, (
                        f"audit tables missing: {sorted(tables)}"
                    )
                evt = AuditEvent(
                    event_type="user_task.started", service="core", outcome=SUCCEEDED,
                    tenant_id="4242", task_id="t-probe", feature_id="user_tasks",
                    action="probe",
                )
                assert await repo.record(evt)
                await repo.enqueue_for_publication(evt)
                async with pool.acquire() as con:
                    n = await con.fetchval(
                        "SELECT COUNT(*) FROM audit_events WHERE event_id=$1", evt.event_id)
                    assert n == 1
                    pending = await con.fetchval(
                        "SELECT COUNT(*) FROM audit_outbox WHERE event_id=$1", evt.event_id)
                    assert pending == 1
                # Tenant scoping: the query path serves only the asking tenant.
                rows = await repo.query(tenant_id="4242", limit=10)
                assert any(r.get("event_id") == evt.event_id for r in rows)
                other = await repo.query(tenant_id="9999", limit=10)
                assert not any(r.get("event_id") == evt.event_id for r in other), (
                    "cross-tenant audit leak"
                )
            finally:
                await pool.close()

        asyncio.run(body())
