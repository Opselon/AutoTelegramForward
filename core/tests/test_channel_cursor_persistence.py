"""Cursor persistence must be atomic and must not mask its own failures.

Production logged this thirteen times in six hours:

    WARNING atf.boot: poll: cannot persist cursors: cannot rollback - no
    transaction is active

The cause was the hand-rolled `BEGIN` / `ROLLBACK` pair in
`SqliteChannelCursorRepository.save_channel_cursors`: the connection is shared,
so when another writer already had a transaction open the `BEGIN` raised
"cannot start a transaction within a transaction", and the handler's `ROLLBACK`
then raised "cannot rollback - no transaction is active" — replacing the real
error with a misleading one, and losing the cursor write entirely. The poller
then re-read the channel tail on every sweep.

These tests pin the three properties that fix depends on: the cursors round-trip,
a failed write changes nothing and reports its own error, and no bare transaction
control statement is ever issued on the shared connection.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteChannelCursorRepository,
)


@pytest.fixture()
def repo(tmp_path):
    db = SqliteDatabase(str(tmp_path / "cursors.db"))
    return db, SqliteChannelCursorRepository(db)


@pytest.mark.asyncio
async def test_cursors_round_trip_and_upsert(repo):
    db, cursors = repo

    await cursors.save_channel_cursors({"-1001": 41, "-1002": 7})
    assert await cursors.load_channel_cursors() == {"-1001": 41, "-1002": 7}

    # A later sweep advances one cursor and adds another: the same key updates.
    await cursors.save_channel_cursors({"-1001": 55, "-1003": 3})
    assert await cursors.load_channel_cursors() == {
        "-1001": 55,
        "-1002": 7,
        "-1003": 3,
    }


@pytest.mark.asyncio
async def test_empty_write_is_a_no_op(repo):
    db, cursors = repo
    await cursors.save_channel_cursors({})
    assert await cursors.load_channel_cursors() == {}


@pytest.mark.asyncio
async def test_a_failed_write_changes_nothing_and_keeps_its_own_error(repo):
    """The whole batch is one unit, and the raised error is the real one."""
    db, cursors = repo
    await cursors.save_channel_cursors({"-1001": 41})

    # The second entry cannot be coerced to an int, so the write fails midway -
    # exactly the shape of any mid-transaction failure.
    with pytest.raises(TypeError):
        await cursors.save_channel_cursors({"-1002": 9, "-1003": object()})

    # "-1002" was written before the failure and must have been rolled back:
    # a half-applied batch would leave the poller skipping or replaying posts.
    assert await cursors.load_channel_cursors() == {"-1001": 41}


@pytest.mark.asyncio
async def test_no_bare_transaction_statements_are_issued(repo):
    """Regression guard for the masking bug itself.

    `BEGIN`/`ROLLBACK` executed directly on the shared connection are what raced
    and produced the misleading warning. Transaction control must stay inside
    `SqliteDatabase.run_in_transaction`.
    """
    db, cursors = repo
    statements: list[str] = []

    class RecordingConnection:
        """sqlite3.Connection is immutable, so record through a thin proxy."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            statements.append(sql if isinstance(sql, str) else str(sql))
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_conn = db._conn
    db._conn = RecordingConnection(real_conn)
    try:
        await cursors.save_channel_cursors({"-1001": 12})
    finally:
        db._conn = real_conn

    upper = [s.strip().upper() for s in statements]
    assert not any(s.startswith("BEGIN") for s in upper), upper
    assert not any(s.startswith("ROLLBACK") for s in upper), upper
    assert any(s.startswith("INSERT INTO CHANNEL_CURSORS") for s in upper), upper


@pytest.mark.asyncio
async def test_a_concurrent_writer_does_not_break_the_save(repo):
    """The original race: another writer mid-transaction on the same connection.

    With the bare `BEGIN` this raised; through `run_in_transaction` the write is
    serialised by the database lock and both writers land.
    """
    import threading

    db, cursors = repo
    started = threading.Event()
    release = threading.Event()

    def other_writer():
        with db._lock:
            db._conn.execute(
                "INSERT INTO channel_cursors (chat_id, last_message_id, updated_at)"
                " VALUES (?,?,?)",
                ("-2001", 1, int(time.time())),
            )
            started.set()
            release.wait(5)
            db._conn.commit()

    thread = threading.Thread(target=other_writer)
    thread.start()
    assert started.wait(5)
    # Nobody has started an explicit transaction, but the connection is busy:
    # the save must wait for the lock instead of failing.
    task = cursors.save_channel_cursors({"-1001": 12})
    release.set()
    thread.join(5)
    await task

    assert await cursors.load_channel_cursors() == {"-2001": 1, "-1001": 12}
