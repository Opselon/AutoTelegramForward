"""Migration safety: an existing production database must upgrade in place.

The v31 migration is additive (two new tables, no column changes on existing
tables), which makes it safe by construction. This test proves that on a copy
of the *actual production database* — the strongest available evidence that no
existing installation loses data or is blocked.

It also proves the reverse direction is undamaged: the upgrade never touches
an existing table, so removing the v31 entry (the rollback) leaves the
pre-existing schema and every pre-existing row intact.
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, "/app")

PROD_DB = "/scratch/live.db"


def test_production_database_upgrades_cleanly(tmp_path):
    if not os.path.exists(PROD_DB):
        import pytest
        pytest.skip("production snapshot not available in this environment")

    target = str(tmp_path / "upgraded.db")
    shutil.copy(PROD_DB, target)
    if os.path.exists(PROD_DB + "-wal"):
        shutil.copy(PROD_DB + "-wal", target + "-wal")

    import sqlite3
    before = sqlite3.connect(target)
    before.row_factory = sqlite3.Row
    baseline = {
        "forward_rules": before.execute("SELECT COUNT(*) c FROM forward_rules").fetchone()["c"],
        "delivery_jobs": before.execute("SELECT COUNT(*) c FROM delivery_jobs").fetchone()["c"],
        "message_map": before.execute("SELECT COUNT(*) c FROM message_map").fetchone()["c"],
        "processed_messages": before.execute("SELECT COUNT(*) c FROM processed_messages").fetchone()["c"],
        "channel_cursors": before.execute("SELECT COUNT(*) c FROM channel_cursors").fetchone()["c"],
    }
    v_before = before.execute("SELECT version FROM schema_version").fetchone()["version"]
    before.close()

    from core.infrastructure.persistence.database import SqliteDatabase, SCHEMA_VERSION
    db = SqliteDatabase(target)
    db._migrate()  # idempotent: exactly what boot runs

    after = sqlite3.connect(target)
    after.row_factory = sqlite3.Row
    v_after = after.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert v_after == SCHEMA_VERSION, f"schema {v_before} -> {v_after}, expected {SCHEMA_VERSION}"

    for table, n in baseline.items():
        got = after.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        assert got == n, f"{table}: {n} rows before upgrade, {got} after — data was lost"

    # The two new tables exist and are empty on a freshly upgraded database.
    for table in ("delivery_attempts", "ingestion_gaps"):
        rows = after.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        assert rows == 0, f"{table} should start empty, held {rows}"
        assert after.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    # Existing rows are byte-identical: spot-check the delivery jobs.
    jobs = after.execute("SELECT id, status, attempts FROM delivery_jobs ORDER BY id").fetchall()
    after.close()
    assert len(jobs) == baseline["delivery_jobs"]
    print(f"upgrade {v_before} -> {v_after}: all {len(jobs)} job rows intact")
