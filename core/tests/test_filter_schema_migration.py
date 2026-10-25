"""SQLite filter_rules schema regression tests (fresh, legacy and healed DBs).

Two related fragilities in the v30 filter migration:

1. ``filter_rules.is_enabled`` was deliberately left out of the migration's
   ``CREATE TABLE IF NOT EXISTS`` and added only by the additive-columns loop.
   A fresh install therefore depended on a *second* code path for a column
   the repository writes on its very first INSERT — and that loop silently
   swallowed every ALTER failure, so a database that could not be upgraded
   reached the repository with a missing column and crashed on use with no
   diagnostic. The column is now declared in the migration *and* added by the
   guarded loop; the loop now logs failures that are not the expected
   duplicate-column race.

2. ``SCHEMA_VERSION`` is derived from ``MIGRATIONS`` so a new migration can
   never again drift ahead of the constant the live database is compared
   against — itself a defect this codebase already shipped once.

These tests run a fresh install, a pre-v30 database upgraded in place, and a
database restored from a pre-v30 backup (no filter_rules table at all), and
assert all three land on a schema the repository can write to immediately.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from core.infrastructure.persistence.database import (
    _ADDITIVE_COLUMNS,
    MIGRATIONS,
    SCHEMA_VERSION,
    SqliteDatabase,
)
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteFilterRuleRepository,
)
from core.domain.entities import FilterRule

OWNER = 5094837833

# Every column the repository writes; the union of the migration's CREATE
# TABLE and the additive loop that heals an older table.
_EXPECTED_FILTER_COLUMNS = {
    "id", "name", "whitelist_keywords", "blacklist_keywords", "regex_patterns",
    "allowed_media_types", "blocked_media_types", "drop_service_messages",
    "min_message_length", "max_message_length", "owner_user_id",
    "regex_block", "regex_require", "is_enabled",
}


def _columns(conn: sqlite3.Connection, table: str = "filter_rules") -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_filter_and_read_back(db: SqliteDatabase, filter_id: str, enabled: bool) -> bool:
    async def body() -> None:
        repo = SqliteFilterRuleRepository(db)
        await repo.add(FilterRule(
            id=filter_id, owner_user_id=OWNER,
            blacklist_keywords=["spam"], is_enabled=enabled))
        back = await repo.get_by_id(filter_id)
        assert back is not None and back.is_enabled is enabled, (
            f"is_enabled did not round-trip on this database (expected {enabled})"
        )

    import asyncio
    asyncio.run(body())
    return True


# --------------------------------------------------------------------------- #

def test_schema_version_is_derived_from_the_migrations():
    assert SCHEMA_VERSION == max(MIGRATIONS), (
        "SCHEMA_VERSION must stay derived: a hand-maintained constant drifted "
        "ahead of the live migrations once already"
    )


def test_the_v30_migration_declares_is_enabled_for_fresh_installs():
    """A fresh database must carry is_enabled from the migration itself."""
    ddl = MIGRATIONS[30]
    assert "is_enabled INTEGER NOT NULL DEFAULT 1" in ddl, (
        "the v30 CREATE TABLE must declare is_enabled so a fresh install does "
        "not depend on the additive loop for a column written by the first INSERT"
    )


def test_additive_columns_cover_every_repository_written_filter_column():
    """Every column in the additive loop plus the v30 CREATE must give the
    shape the repository writes — no more, no missing."""
    create_cols = {
        "id", "name", "whitelist_keywords", "blacklist_keywords", "regex_patterns",
        "allowed_media_types", "blocked_media_types", "drop_service_messages",
        "min_message_length", "max_message_length",
    }
    additive = {c for t, c, _, _ in _ADDITIVE_COLUMNS if t == "filter_rules"}
    assert (create_cols | additive) == _EXPECTED_FILTER_COLUMNS, (
        f"filter_rules shape drift: {(create_cols | additive) ^ _EXPECTED_FILTER_COLUMNS}"
    )


def test_fresh_install_writes_and_reads_is_enabled():
    with tempfile.TemporaryDirectory() as d:
        db = SqliteDatabase(str(Path(d) / "fresh.db"))
        try:
            assert db.get_schema_version() == SCHEMA_VERSION
            assert _columns(db.connection) == _EXPECTED_FILTER_COLUMNS
            assert _add_filter_and_read_back(db, "fresh-on", True)
            assert _add_filter_and_read_back(db, "fresh-off", False)
        finally:
            db.close()


def test_pre_v30_database_upgrades_and_keeps_working():
    """A legacy v29 database upgrades in place; existing rows stay enabled."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "legacy.db"
        conn = sqlite3.connect(str(path))
        try:
            # Build a pre-v30 filter_rules by hand (the v11 CREATE + the two
            # additive columns that existed before v30).
            conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (29);
            CREATE TABLE filter_rules (
                id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                whitelist_keywords TEXT NOT NULL DEFAULT '[]',
                blacklist_keywords TEXT NOT NULL DEFAULT '[]',
                regex_patterns TEXT NOT NULL DEFAULT '[]',
                allowed_media_types TEXT NOT NULL DEFAULT '[]',
                blocked_media_types TEXT NOT NULL DEFAULT '[]',
                drop_service_messages INTEGER NOT NULL DEFAULT 1,
                min_message_length INTEGER NOT NULL DEFAULT 0,
                max_message_length INTEGER NOT NULL DEFAULT 0
            );
            ALTER TABLE filter_rules ADD COLUMN owner_user_id INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE filter_rules ADD COLUMN regex_block TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE filter_rules ADD COLUMN regex_require TEXT NOT NULL DEFAULT '[]';
            """)
            # A filter that already exists: it must stay enabled (legacy default).
            conn.execute(
                "INSERT INTO filter_rules (id, owner_user_id, blacklist_keywords)"
                " VALUES ('legacy-row', ?, '[]')", (OWNER,))
            conn.commit()
        finally:
            conn.close()

        db = SqliteDatabase(str(path))
        try:
            assert db.get_schema_version() == SCHEMA_VERSION, "upgrade did not run"
            assert _columns(db.connection) == _EXPECTED_FILTER_COLUMNS
            # The legacy row is healed to enabled — never silently disabled.
            async def read() -> bool:
                repo = SqliteFilterRuleRepository(db)
                back = await repo.get_by_id("legacy-row")
                return bool(back and back.is_enabled)
            import asyncio
            assert asyncio.run(read()) is True, (
                "a pre-v30 filter must heal to enabled, not silently switch off"
            )
            # And new writes of both states work on the upgraded schema.
            assert _add_filter_and_read_back(db, "up-on", True)
            assert _add_filter_and_read_back(db, "up-off", False)
        finally:
            db.close()


def test_database_without_the_filter_table_still_bootstraps():
    """An install that never created a saved filter has no table at v30."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "bare.db"
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            conn.execute("INSERT INTO schema_version (version) VALUES (29)")
            # Only the tables some other part of v29 needs.
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()

        db = SqliteDatabase(str(path))
        try:
            assert db.get_schema_version() == SCHEMA_VERSION
            assert _columns(db.connection) == _EXPECTED_FILTER_COLUMNS, (
                "a database that had no filter_rules table must still end up "
                "with the full current shape"
            )
            assert _add_filter_and_read_back(db, "bare-off", False)
        finally:
            db.close()


def test_migration_loop_is_idempotent_on_a_current_database():
    """Re-opening an up-to-date database must not raise or duplicate."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "twice.db"
        db = SqliteDatabase(str(path))
        db.close()
        db = SqliteDatabase(str(path))
        try:
            assert db.get_schema_version() == SCHEMA_VERSION
            assert _columns(db.connection) == _EXPECTED_FILTER_COLUMNS
        finally:
            db.close()
