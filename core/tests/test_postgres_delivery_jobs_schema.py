"""PostgreSQL delivery-jobs schema regression tests (real server, both paths).

The durable delivery queue was broken on every PostgreSQL deployment in two
complementary ways, both silent:

1. **Missing column.** ``initialize_postgres_schema`` created
   ``delivery_jobs`` *without* ``job_kind``, while
   ``PostgresDeliveryQueueRepository.enqueue`` inserts ``job_kind`` and
   resolves duplicates against
   ``ON CONFLICT(rule_id, source_chat_id, source_message_id, target_chat_id,
   job_kind)``. The very first enqueue raised ``UndefinedColumnError``. The
   SQLite twin was rebuilt to carry the column (migration v20); the PostgreSQL
   bootstrap was never updated.

2. **Missing constraint.** Even where a legacy PostgreSQL table carried the
   column, the UNIQUE key stayed four columns wide, so a keyword reply to the
   same (rule, source, target) as an existing forward hit
   ``ON CONFLICT ... DO NOTHING`` and was dropped — the reply never shipped
   while the pipeline logged a successful enqueue. SQLite rebuilt the table
   for the same reason; PostgreSQL can swap the constraint in place.

The deeper root cause for (2): the guarded ``DO $$ ... $$`` constraint swap is
a *procedural* block whose body contains semicolons **and** single quotes.
``_split_sql_script`` tracked single-quoted strings and comments but not
PostgreSQL dollar quotes, so it closed the quote at the first ``'`` inside the
body and split the block into fragments — which the bootstrap swallowed with
``except`` + warning. These tests split both ways: pure-unit coverage of the
splitter's dollar-quote handling, and real-PostgreSQL coverage of the upgrade
and fresh-install paths (row preservation, dedup semantics, claim, and
bootstrap idempotency).

Set ``ATF_TEST_POSTGRES_URL`` to run the real-server tests (CI sets this
against a PostgreSQL service). They are skipped otherwise.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import re

import pytest

from core.infrastructure.persistence import postgres_repositories as pg

DSN = os.environ.get("ATF_TEST_POSTGRES_URL") or os.environ.get("ATF_DATABASE_URL") or ""

requires_pg = pytest.mark.skipif(
    not DSN,
    reason="Real PostgreSQL unavailable — set ATF_TEST_POSTGRES_URL",
)


# The shape every delivery_jobs table must have: the columns the repository
# writes, and the dedup key its ON CONFLICT resolves against.
_REQUIRED_COLUMNS = (
    "id", "rule_id", "source_chat_id", "source_message_id", "target_chat_id",
    "payload_json", "status", "attempts", "max_attempts", "next_retry_at",
    "lease_until", "worker_id", "error_detail", "created_at", "updated_at",
    "intermediate_chat_id", "intermediate_message_id", "delivery_stage",
    "job_kind",
)
_UNIQUE_COLS = ("rule_id", "source_chat_id", "source_message_id", "target_chat_id", "job_kind")


def _delivery_jobs_create(sql: str) -> str | None:
    m = re.search(r"CREATE TABLE IF NOT EXISTS delivery_jobs \((.*?)\n        \)", sql, re.S)
    return m.group(1) if m else None


def _declared_unique_body(sql: str) -> str | None:
    body = _delivery_jobs_create(sql)
    if body is None:
        return None
    m = re.search(r"CONSTRAINT delivery_jobs_unique\s+UNIQUE\s*\(([^)]*)\)", body, re.S)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Pure unit: the bootstrap DDL declares the shape the repository writes
# --------------------------------------------------------------------------- #

class TestBootstrapDeclaresJobKind:
    def test_create_table_carries_job_kind(self):
        body = _delivery_jobs_create(inspect.getsource(pg.initialize_postgres_schema))
        assert body is not None, "delivery_jobs CREATE TABLE missing from the bootstrap"
        assert re.search(r"^\s+job_kind\s+TEXT NOT NULL DEFAULT 'forward'\s*,?\s*$", body, re.M), (
            "job_kind must be declared in the delivery_jobs CREATE TABLE — "
            "without it, every enqueue on a fresh PostgreSQL database raises "
            "UndefinedColumnError"
        )

    def test_unique_key_includes_job_kind(self):
        body = _declared_unique_body(inspect.getsource(pg.initialize_postgres_schema))
        assert body is not None, "delivery_jobs_unique constraint not declared"
        cols = [c.strip() for c in body.split(",")]
        assert cols == list(_UNIQUE_COLS), (
            f"dedup key drift: {cols} — a keyword reply to the same target as "
            "an existing forward is dropped by ON CONFLICT DO NOTHING"
        )

    def test_ddl_references_match_the_repository_writes(self):
        """Every column the repository INSERTs must be declared."""
        src = inspect.getsource(pg)
        m = re.search(
            r"INSERT INTO delivery_jobs \s*\(([^)]*)\)",
            src.replace('"\n                    "', " "),
        )
        assert m, "delivery_jobs INSERT not found in the repository"
        written = {c.strip() for c in m.group(1).split(",") if c.strip()}
        declared = set(_REQUIRED_COLUMNS)
        missing = written - declared
        assert not missing, f"repository writes undeclared columns: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# Pure unit: dollar-quote handling in the statement splitter
# --------------------------------------------------------------------------- #

class TestDollarQuoteSplitting:
    """``DO $$ ... $$`` blocks must survive the splitter intact.

    The guarded constraint swap is the only statement in the bootstrap that
    needs dollar quoting, and its body contains both semicolons and single
    quotes (the LIKE pattern). A scanner that only tracks single-quoted
    strings ends the quote at ``'%...%'`` and then treats the body's
    semicolons as statement boundaries, shipping fragments to the server.
    """

    def test_do_block_with_semicolons_stays_one_statement(self):
        script = "DO $$ BEGIN PERFORM 1; PERFORM 2; END $$;"
        assert pg._split_sql_script(script) == ["DO $$ BEGIN PERFORM 1; PERFORM 2; END $$"]

    def test_do_block_with_single_quotes_stays_one_statement(self):
        script = (
            "DO $$\n"
            "BEGIN\n"
            "    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='c'"
            "      AND pg_get_constraintdef(oid) LIKE '%job_kind%') THEN\n"
            "        ALTER TABLE t DROP CONSTRAINT IF EXISTS c;\n"
            "        ALTER TABLE t ADD CONSTRAINT c UNIQUE (a, b);\n"
            "    END IF;\n"
            "END $$;"
        )
        got = pg._split_sql_script(script)
        assert len(got) == 1, f"DO block fragmentated: {got}"
        assert got[0].startswith("DO $$") and got[0].endswith("END $$")

    def test_tagged_dollar_quotes_pair_with_their_own_tag(self):
        script = "SELECT $tag$ a $tag$; SELECT 2;"
        assert pg._split_sql_script(script) == ["SELECT $tag$ a $tag$", "SELECT 2"]

    def test_dollar_quote_body_is_not_scanned_for_string_quotes(self):
        """A ``'`` inside a dollar-quoted body is plain text, not a boundary."""
        script = "DO $body$ BEGIN RAISE NOTICE 'it''s ; fine'; END $body$; SELECT 3;"
        got = pg._split_sql_script(script)
        assert len(got) == 2
        assert got[0] == "DO $body$ BEGIN RAISE NOTICE 'it''s ; fine'; END $body$"
        assert got[1] == "SELECT 3"

    def test_a_lone_dollar_sign_is_not_a_quote(self):
        """``$`` in an operator context must not start a quote."""
        assert pg._split_sql_script("SELECT 1 $ 2; SELECT 3;") == [
            "SELECT 1 $ 2", "SELECT 3",
        ]

    def test_splitter_keeps_the_comment_and_string_behaviour_it_fixed(self):
        """The original defect classes stay fixed alongside the new one."""
        script = (
            "-- the contract is `last_error` on both backends.\n"
            "CREATE TABLE IF NOT EXISTS t (a TEXT);\n"
            "INSERT INTO t VALUES ('O''Brien; yes');\n"
        )
        got = pg._split_sql_script(script)
        assert len(got) == 2
        assert got[0].startswith("CREATE TABLE")
        assert "O''Brien; yes" in got[1]


# --------------------------------------------------------------------------- #
# Real PostgreSQL: legacy upgrade preserves rows, fresh install works,
# bootstrap is idempotent, and the dedup semantics are the documented ones.
# --------------------------------------------------------------------------- #

# A pre-v20 PostgreSQL delivery_jobs table: no job_kind, four-column key.
_LEGACY_DDL = """
CREATE TABLE IF NOT EXISTS delivery_jobs (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id BIGINT NOT NULL,
    target_chat_id TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_retry_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    worker_id TEXT NOT NULL DEFAULT '',
    error_detail TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    intermediate_chat_id TEXT NOT NULL DEFAULT '',
    intermediate_message_id BIGINT,
    delivery_stage TEXT NOT NULL DEFAULT 'DIRECT',
    CONSTRAINT delivery_jobs_unique UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id)
);
"""


@requires_pg
class TestDeliveryJobsOnRealPostgres:
    """Runs against a real PostgreSQL (ATF_TEST_POSTGRES_URL)."""

    def _pool(self):
        import asyncpg
        return asyncio.run(asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4))

    def test_legacy_database_upgrades_without_losing_rows(self):
        from core.domain.entities import DeliveryJob
        import asyncpg

        async def body():
            pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
            try:
                async with pool.acquire() as con:
                    await con.execute("DROP TABLE IF EXISTS delivery_jobs CASCADE")
                    await con.execute(_LEGACY_DDL)
                    # A production-like backlog: a forward that already shipped.
                    await con.execute(
                        "INSERT INTO delivery_jobs (id, rule_id, source_chat_id,"
                        " source_message_id, target_chat_id, payload_json, status,"
                        " created_at, updated_at)"
                        " VALUES ('legacy-1','r-legacy','-1001',707,'-1002','{}','SENT',11,12)"
                    )
                # The upgrade under test.
                await pg.initialize_postgres_schema(pool)
                async with pool.acquire() as con:
                    cols = {r["column_name"] for r in await con.fetch(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name='delivery_jobs'")}
                    assert "job_kind" in cols, "upgrade lost the job_kind column"
                    kept = await con.fetchval(
                        "SELECT COUNT(*) FROM delivery_jobs WHERE id='legacy-1'")
                    assert kept == 1, (
                        "row-preserving upgrade: the existing backlog must survive"
                    )
                    kind = await con.fetchval(
                        "SELECT job_kind FROM delivery_jobs WHERE id='legacy-1'")
                    assert kind == "forward", (
                        f"legacy rows carry the 'forward' default, got {kind!r}"
                    )
                    key = await con.fetchrow(
                        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint"
                        " WHERE conname='delivery_jobs_unique'")
                    assert key is not None and "job_kind" in key["d"], (
                        "dedup key was not widened by the upgrade"
                    )
            finally:
                await pool.close()

        asyncio.run(body())

    def test_forward_and_keyword_reply_to_same_target_both_enqueue(self):
        """The exact defect: the reply was dropped by ON CONFLICT DO NOTHING."""
        from core.domain.entities import DeliveryJob

        async def body():
            import asyncpg
            pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
            try:
                await pg.initialize_postgres_schema(pool)
                async with pool.acquire() as con:
                    await con.execute("DELETE FROM delivery_jobs WHERE rule_id='rr-probe'")
                repo = pg.PostgresDeliveryQueueRepository(pool)
                ok_f = await repo.enqueue(DeliveryJob(
                    id="probe-f", rule_id="rr-probe", source_chat_id="-1001",
                    source_message_id=1, target_chat_id="-1002"))
                ok_r = await repo.enqueue(DeliveryJob(
                    id="probe-r", rule_id="rr-probe", source_chat_id="-1001",
                    source_message_id=1, target_chat_id="-1002", job_kind="keyword_reply"))
                assert ok_f and ok_r, (
                    "a forward and its keyword reply to the same target must both enqueue"
                )
                async with pool.acquire() as con:
                    n = await con.fetchval(
                        "SELECT COUNT(*) FROM delivery_jobs WHERE rule_id='rr-probe'")
                    assert n == 2, f"expected 2 rows, got {n}"
                    kinds = sorted(await con.fetch(
                        "SELECT job_kind FROM delivery_jobs WHERE rule_id='rr-probe'"))
                    assert [r["job_kind"] for r in kinds] == ["forward", "keyword_reply"]
                # A duplicate of the forward is ignored, not an error.
                dup = await repo.enqueue(DeliveryJob(
                    id="probe-f2", rule_id="rr-probe", source_chat_id="-1001",
                    source_message_id=1, target_chat_id="-1002"))
                assert not dup, "duplicate forward must be ignored by the dedup key"
            finally:
                await pool.close()

        asyncio.run(body())

    def test_repeated_bootstrap_is_a_no_op(self):
        """The guarded constraint swap must not rebuild the index every boot."""
        import asyncpg

        async def body():
            pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
            try:
                await pg.initialize_postgres_schema(pool)
                async with pool.acquire() as con:
                    oid = await con.fetchval(
                        "SELECT oid FROM pg_constraint WHERE conname='delivery_jobs_unique'")
                    idx = await con.fetchval(
                        "SELECT conindid FROM pg_constraint WHERE conname='delivery_jobs_unique'")
                for _ in range(3):
                    await pg.initialize_postgres_schema(pool)
                    async with pool.acquire() as con:
                        assert oid == await con.fetchval(
                            "SELECT oid FROM pg_constraint"
                            " WHERE conname='delivery_jobs_unique'"), (
                            "constraint was rebuilt — the guard is not idempotent"
                        )
                        assert idx == await con.fetchval(
                            "SELECT conindid FROM pg_constraint"
                            " WHERE conname='delivery_jobs_unique'"), (
                            "constraint index was rebuilt — the guard is not idempotent"
                        )
            finally:
                await pool.close()

        asyncio.run(body())

    def test_claim_and_mark_sent_round_trip(self):
        from core.domain.entities import DeliveryJob

        async def body():
            import asyncpg
            pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
            try:
                await pg.initialize_postgres_schema(pool)
                async with pool.acquire() as con:
                    # Only this test's rows participate: claim_batch has no
                    # rule filter, and other tests in this class leave
                    # claimed/sent rows behind in the shared database.
                    await con.execute("DELETE FROM delivery_jobs WHERE rule_id='rr-claim'")
                    # Make the backlog look stale so a leftover from another
                    # test cannot be re-claimed here.
                    await con.execute(
                        "UPDATE delivery_jobs SET status='SENT', lease_until=0"
                        " WHERE status IN ('PENDING','RETRY_WAIT','CLAIMED')"
                        "   AND rule_id <> 'rr-claim'")
                repo = pg.PostgresDeliveryQueueRepository(pool)
                for i, kind in enumerate(("forward", "keyword_reply")):
                    await repo.enqueue(DeliveryJob(
                        id=f"claim-{i}", rule_id="rr-claim", source_chat_id="-1001",
                        source_message_id=10 + i, target_chat_id="-1005",
                        job_kind=kind))
                # The queue serializes per destination ("one in-flight job per
                # target"), so the first claim takes exactly one row even
                # though both are eligible; the second appears only after the
                # first is marked SENT. Asserting the contract, not a batch
                # of two, keeps this test honest about the claim path.
                first = await repo.claim_batch("worker-probe", 10, 30.0)
                assert {j.id for j in first} == {"claim-0"}, (
                    f"per-target serialization broken: {sorted(j.id for j in first)}"
                )
                assert first[0].job_kind == "forward"
                assert await repo.mark_sent(first[0].id, 900)
                second = await repo.claim_batch("worker-probe", 10, 30.0)
                assert {j.id for j in second} == {"claim-1"}, (
                    f"the next job in the lane must claim after the first ships:"
                    f" {sorted(j.id for j in second)}"
                )
                assert second[0].job_kind == "keyword_reply"
                assert await repo.mark_sent(second[0].id, 901)
                async with pool.acquire() as con:
                    left = await con.fetchval(
                        "SELECT COUNT(*) FROM delivery_jobs"
                        " WHERE rule_id='rr-claim' AND status='SENT'")
                    assert left == 2
            finally:
                await pool.close()

        asyncio.run(body())
