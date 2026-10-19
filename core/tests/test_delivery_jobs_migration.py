"""Part 2 STEER 06 — the delivery_jobs deliverable-key migration, on a real file.

The queue dedups with ``INSERT OR IGNORE`` on a UNIQUE key. That key used to be
(rule, source_chat, source_message, target) with no room for *what* is being
delivered, so the keyword reply to a target the source was also forwarded to
collided with the forward's row and was dropped in silence.

These tests build the old schema by hand (exactly as a pre-v20 install has it),
open it through ``SqliteDatabase`` and assert that: nothing is lost, the key is
widened, the kind of the rows already queued is derived from the row itself, and
the migration is a no-op the second time.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from core.infrastructure.persistence.database import SqliteDatabase

LEGACY_DDL = """
CREATE TABLE delivery_jobs (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id INTEGER NOT NULL,
    target_chat_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_retry_at REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    worker_id TEXT,
    error_detail TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(rule_id, source_chat_id, source_message_id, target_chat_id)
);
"""


def _make_legacy_db(path: str, rows: list[tuple[str, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_DDL)
    now = int(time.time())
    for job_id, text in rows:
        conn.execute(
            "INSERT INTO delivery_jobs (id, rule_id, source_chat_id, source_message_id,"
            " target_chat_id, payload_json, status, created_at, updated_at)"
            " VALUES (?, 'r-1', '-100', 1, '-200', ?, 'PENDING', ?, ?)",
            (job_id, '{"text": "%s"}' % text, now, now),
        )
    conn.commit()
    conn.close()


def _schema_sql(db: SqliteDatabase) -> str:
    row = db.query_one(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='delivery_jobs'"
    )
    return "".join(str(row["sql"]).split()).lower()


def test_a_legacy_table_is_rebuilt_with_its_rows_intact(tmp_path):
    path = str(tmp_path / "legacy.db")
    # The legacy key can hold exactly one row for (rule, source, msg, target) —
    # that is the defect. Seed the forward, migrate, then store the reply.
    _make_legacy_db(path, [("r-1:-100:1:-200", "forward body")])

    db = SqliteDatabase(path)
    try:
        assert "unique(rule_id,source_chat_id,source_message_id,target_chat_id,job_kind)" in _schema_sql(db), _schema_sql(db)
        rows = {r["id"]: r for r in db.query_all("SELECT * FROM delivery_jobs")}
        assert len(rows) == 1, rows
        assert rows["r-1:-100:1:-200"]["job_kind"] == "forward"
        assert "forward body" in rows["r-1:-100:1:-200"]["payload_json"]
        # The hop columns the worker reads must exist on the rebuilt table.
        for column in ("intermediate_chat_id", "intermediate_message_id", "delivery_stage"):
            assert column in rows["r-1:-100:1:-200"], column
        db.execute(
            "INSERT OR IGNORE INTO delivery_jobs (id, rule_id, source_chat_id,"
            " source_message_id, target_chat_id, payload_json, status, created_at,"
            " updated_at, job_kind) VALUES (?, 'r-1', '-100', 1, '-200', ?, 'PENDING',"
            " 1, 1, 'keyword_reply')",
            ("r-1:-100:1:-200:keyword_reply", '{"text": "reply body"}'),
        )
        after = {r["id"]: r for r in db.query_all("SELECT * FROM delivery_jobs")}
        assert set(after) == {"r-1:-100:1:-200", "r-1:-100:1:-200:keyword_reply"}, after
        assert after["r-1:-100:1:-200:keyword_reply"]["job_kind"] == "keyword_reply"
    finally:
        db.close()


def test_the_kind_of_an_already_queued_reply_row_is_derived_from_its_id(tmp_path):
    """A pre-migration table cannot contain a reply row, so prove the backfill
    expression itself: a row whose id carries the kind suffix is labelled."""
    path = str(tmp_path / "legacy.db")
    _make_legacy_db(path, [("r-1:-100:1:-200:keyword_reply", "reply body")])

    db = SqliteDatabase(path)
    try:
        row = db.query_one("SELECT job_kind, payload_json FROM delivery_jobs")
        assert row["job_kind"] == "keyword_reply", row
        assert "reply body" in row["payload_json"]
    finally:
        db.close()


def test_the_old_collision_can_no_longer_happen(tmp_path):
    """The exact insert that used to be ignored now stores a second row."""
    path = str(tmp_path / "legacy.db")
    _make_legacy_db(path, [("r-1:-100:1:-200", "forward body")])

    db = SqliteDatabase(path)
    try:
        db.execute(
            "INSERT OR IGNORE INTO delivery_jobs (id, rule_id, source_chat_id,"
            " source_message_id, target_chat_id, payload_json, status, created_at,"
            " updated_at, job_kind) VALUES (?, 'r-1', '-100', 1, '-200', '{}',"
            " 'PENDING', 1, 1, ?)",
            ("r-1:-100:1:-200:keyword_reply", "keyword_reply"),
        )
        assert len(db.query_all("SELECT * FROM delivery_jobs")) == 2
    finally:
        db.close()


def test_a_replay_is_still_ignored_after_the_migration(tmp_path):
    """Widening the key must not turn idempotency off."""
    path = str(tmp_path / "legacy.db")
    _make_legacy_db(path, [("r-1:-100:1:-200", "forward body")])

    db = SqliteDatabase(path)
    try:
        for _ in range(3):
            db.execute(
                "INSERT OR IGNORE INTO delivery_jobs (id, rule_id, source_chat_id,"
                " source_message_id, target_chat_id, payload_json, status, created_at,"
                " updated_at, job_kind) VALUES (?, 'r-1', '-100', 1, '-200', '{}',"
                " 'PENDING', 1, 1, 'forward')",
                ("r-1:-100:1:-200",),
            )
        assert len(db.query_all("SELECT * FROM delivery_jobs")) == 1
    finally:
        db.close()


def test_reopening_a_migrated_database_changes_nothing(tmp_path):
    path = str(tmp_path / "legacy.db")
    _make_legacy_db(path, [("r-1:-100:1:-200", "forward body")])

    first = SqliteDatabase(path)
    before = _schema_sql(first)
    count = first.query_all("SELECT * FROM delivery_jobs")
    first.close()

    second = SqliteDatabase(path)
    try:
        assert _schema_sql(second) == before
        assert len(second.query_all("SELECT * FROM delivery_jobs")) == len(count) == 1
    finally:
        second.close()


def test_a_fresh_database_is_born_with_the_widened_key(tmp_path):
    db = SqliteDatabase(str(tmp_path / "fresh.db"))
    try:
        assert "unique(rule_id,source_chat_id,source_message_id,target_chat_id,job_kind)" in _schema_sql(db)
    finally:
        db.close()
