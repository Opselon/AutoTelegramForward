"""PostgreSQL schema/statement-splitting regression tests.

Two defects found while hardening the per-user task lifecycle, both fixed
server-side and pinned here:

1. `initialize_postgres_schema` shipped comment fragments to the server as
   SQL because the script was split on every ";". Two `--` comment lines
   contain a semicolon, which cut the `dead_letter_queue` and
   `web_login_codes` CREATE TABLE statements in half: those tables were never
   created on a PostgreSQL deployment and the resulting errors were swallowed
   by a bare `except: pass`.
2. The PostgreSQL bootstrap never created `user_tasks` at all, although
   main.py binds PostgresUserTaskRepository whenever ATF_DATABASE_URL is set.

The first is a pure-unit regression test (no server needed) and would have
failed before the fix. The second is checked by parity against the SQLite
schema, and both are additionally exercised against a real PostgreSQL when
ATF_TEST_POSTGRES_URL is set:

    ATF_TEST_POSTGRES_URL=postgresql://user:pw@127.0.0.1:55432/db \\
        pytest core/tests/test_postgres_schema_parity.py

Run: pytest core/tests/test_postgres_schema_parity.py
"""

import inspect
import os
import re

import pytest

from core.infrastructure.persistence import postgres_repositories as pg
from core.infrastructure.persistence.database import MIGRATIONS

DDL_KEYWORDS = ("CREATE", "ALTER", "UPDATE", "INSERT", "DELETE", "DROP", "GRANT", "COMMENT")


def _ddl_script() -> str:
    return inspect.getsource(pg.initialize_postgres_schema)


def _ddl_listing() -> str:
    """Only the DDL block listing inside initialize_postgres_schema.

    Sliced from `ddl_statements = [` so the function's own docstring (prose,
    not SQL) is not mistaken for a script block.
    """
    src = _ddl_script()
    marker = "ddl_statements = ["
    assert marker in src, "ddl_statements listing not found"
    return src.split(marker, 1)[1]


def _ddl_blocks() -> list:
    """The SQL script literals themselves (no Python comments or quotes)."""
    blocks = re.findall(r'"""(.*?)"""', _ddl_listing(), re.S)
    assert blocks, "no SQL script literals found in the ddl_statements listing"
    return blocks


def _sqlite_migration_sql(version: int) -> str:
    try:
        return MIGRATIONS[version]
    except KeyError:  # pragma: no cover - schema drift guard
        raise AssertionError(f"migration {version} not found")


# --------------------------------------------------------------------------- #
# Statement splitting (defect 1)
# --------------------------------------------------------------------------- #

class TestScriptSplitting:
    def test_semicolon_inside_a_comment_does_not_split_statements(self):
        script = (
            "-- first; the contract is `last_error` on both backends.\n"
            "CREATE TABLE IF NOT EXISTS t (a TEXT);\n"
            "-- code_hash is SHA-256 of the code; used_at makes it single-use.\n"
            "CREATE TABLE IF NOT EXISTS u (b TEXT);\n"
        )
        statements = pg._split_sql_script(script)
        assert len(statements) == 2, statements
        assert all("comment" not in s for s in statements)
        assert statements[0].startswith("CREATE TABLE IF NOT EXISTS t")
        assert statements[1].startswith("CREATE TABLE IF NOT EXISTS u")

    def test_comments_never_reach_the_server(self):
        script = (
            "-- a comment; with a semicolon\n"
            "CREATE TABLE x (a TEXT);  -- trailing comment\n"
            "/* block; comment */\n"
            "CREATE INDEX i ON x (a);\n"
        )
        for stmt in pg._split_sql_script(script):
            assert not stmt.lstrip().startswith("--"), stmt
            assert "comment" not in stmt, stmt
            assert stmt.startswith(("CREATE TABLE", "CREATE INDEX")), stmt

    def test_semicolon_inside_a_string_literal_is_not_a_boundary(self):
        script = "INSERT INTO t (a) VALUES ('x;y'); INSERT INTO t (a) VALUES ('z');"
        statements = pg._split_sql_script(script)
        assert len(statements) == 2, statements
        assert "'x;y'" in statements[0]

    def test_escaped_quote_inside_a_string_is_handled(self):
        script = "INSERT INTO t (a) VALUES ('it''s; fine'); SELECT 1;"
        statements = pg._split_sql_script(script)
        assert len(statements) == 2, statements
        assert "'it''s; fine'" in statements[0]

    def test_every_shipped_ddl_statement_is_real_sql(self):
        """No fragment of the shipped bootstrap may be a comment or prose.

        This is the assertion that fails on the pre-fix splitter: it produced
        fragments such as '-- code_hash is SHA-256 ...' and
        'used_at makes it single-use.\nCREATE TABLE ...'.
        """
        checked = 0
        for block in _ddl_blocks():
            for stmt in pg._split_sql_script(block):
                assert not stmt.startswith("--"), f"comment shipped as SQL: {stmt[:60]!r}"
                assert stmt.upper().startswith(DDL_KEYWORDS), (
                    f"non-statement fragment shipped as SQL: {stmt[:60]!r}")
                checked += 1
        assert checked > 20, f"expected the full bootstrap, saw {checked} statements"

    def test_the_two_comment_lines_that_caused_the_defect_are_still_covered(self):
        """The exact semicolon-bearing comments must survive as comments."""
        joined = "\n".join(
            s for block in _ddl_blocks() for s in pg._split_sql_script(block))
        assert "on both backends" not in joined
        assert "single-use" not in joined
        # …while the statements they used to break are present and whole.
        assert "CREATE TABLE IF NOT EXISTS dead_letter_queue" in joined
        assert "CREATE TABLE IF NOT EXISTS web_login_codes" in joined


# --------------------------------------------------------------------------- #
# Schema parity (defect 2)
# --------------------------------------------------------------------------- #

class TestUserTasksSchemaParity:
    def test_postgres_bootstrap_creates_user_tasks(self):
        """main.py binds the PG task repository whenever ATF_DATABASE_URL is
        set, so the bootstrap must create the table it writes to."""
        assert re.search(r"CREATE TABLE IF NOT EXISTS\s+user_tasks\b", _ddl_script())

    def test_postgres_columns_match_the_sqlite_migration(self):
        sqlite_sql = _sqlite_migration_sql(24)
        m = re.search(r"CREATE TABLE IF NOT EXISTS user_tasks \((.*?)\n    \)", sqlite_sql, re.S)
        assert m, "SQLite user_tasks DDL not found in migration 24"
        sqlite_cols = _column_names(m.group(1))

        m2 = re.search(r"CREATE TABLE IF NOT EXISTS user_tasks \((.*?)\n        \)", _ddl_script(), re.S)
        assert m2, "PostgreSQL user_tasks DDL not found"
        pg_cols = _column_names(m2.group(1))

        assert pg_cols == sqlite_cols, (
            f"column drift between engines\n  sqlite: {sqlite_cols}\n  pg:     {pg_cols}")

    def test_postgres_creates_the_indexes_the_queries_need(self):
        src = _ddl_script()
        for index in ("idx_user_tasks_idem", "idx_user_tasks_owner", "idx_user_tasks_pick"):
            assert index in src, f"{index} missing from the PostgreSQL bootstrap"

    def test_claim_uses_skip_locked_and_rechecks_status(self):
        """The claim must lock the candidate rather than race for it."""
        sql = inspect.getsource(pg.PostgresUserTaskRepository.acquire_next)
        assert "FOR UPDATE SKIP LOCKED" in sql
        # the outer predicate is what READ COMMITTED re-checks after the lock
        assert sql.count("status IN ('PENDING','RETRY_WAIT')") >= 2, sql


def _column_names(body: str) -> list:
    names = []
    for line in body.split("\n"):
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        token = line.split()[0].rstrip(",")
        if token.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
            continue
        names.append(token)
    return names


# --------------------------------------------------------------------------- #
# Real-server checks (opt-in)
# --------------------------------------------------------------------------- #

DSN = os.environ.get("ATF_TEST_POSTGRES_URL", "")
pg_only = pytest.mark.skipif(not DSN, reason="ATF_TEST_POSTGRES_URL is not set")


@pg_only
class TestAgainstRealPostgres:
    """Exercises the bootstrap and the claim statement on a real server.

    Verified manually against PostgreSQL 16: after the fix all 11 declared
    tables exist, the schema statements log no failures, and two concurrent
    workers claiming from the same queue never receive the same row (the
    pre-fix statement returned the same row to both workers in 19-20 of 20
    rounds).
    """

    async def _pool(self):
        import asyncpg
        return await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)

    async def test_bootstrap_creates_every_declared_table(self):
        import asyncpg  # noqa: F401
        pool = await self._pool()
        try:
            await pg.initialize_postgres_schema(pool)
            declared = set(re.findall(
                r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", _ddl_script()))
            present = {r["tablename"] for r in await pool.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
            missing = sorted(declared - present)
            assert not missing, f"declared but not created: {missing}"
        finally:
            await pool.close()

    async def test_concurrent_claims_never_share_a_task(self):
        import asyncio
        pool = await self._pool()
        pool2 = await self._pool()
        try:
            await pg.initialize_postgres_schema(pool)
            repo_a = pg.PostgresUserTaskRepository(pool)
            repo_b = pg.PostgresUserTaskRepository(pool2)
            for rnd in range(10):
                await pool.execute("DELETE FROM user_tasks")
                for i in range(2):
                    await repo_a.add(_task(f"t{rnd}-{i}", now=1000 + rnd))
                got = await asyncio.gather(
                    repo_a.acquire_next(f"a{rnd}", 9999, 5000),
                    repo_b.acquire_next(f"b{rnd}", 9999, 5000))
                ids = [t.id for t in got if t is not None]
                if len(ids) == 2:
                    assert ids[0] != ids[1], "both workers claimed the same task"
                rows = await pool.fetch("SELECT attempt FROM user_tasks")
                assert all(int(r["attempt"]) == 1 for r in rows), "attempt inflated by a steal"
        finally:
            await pool.close()
            await pool2.close()


def _task(task_id: str, owner: int = 4242, now: int = 1000, key: str = ""):
    from core.domain.entities import UserTask
    return UserTask(
        id=task_id, owner_user_id=owner, kind="dlq.purge", status="PENDING",
        idempotency_key=key or task_id, attempt=0, max_attempts=3,
        lease_token="", lease_until=0, run_at=0, payload_json="{}",
        result_json="", error="", error_code="", created_at=now,
        updated_at=now, started_at=0, finished_at=0)
