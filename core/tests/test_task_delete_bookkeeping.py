"""Deleting a task must take the task's own bookkeeping with it.

A live audit of the shipped database found rows left behind by deleted tasks:
``message_map`` and ``processed_messages`` rows whose ``rule_id`` no longer
exists (7 of them across 2 deleted tasks, plus whatever each deleted task had in
jobs, dead letters, counters and the error log). The repository's ``delete()``
removed the ``forward_rules`` row and nothing else, and nothing reconciles those
tables afterwards.

The user-visible consequence is not the disk space: a leftover ``message_map`` or
``processed_messages`` row *claims* the task already delivered a source message,
so a task that is deleted and re-created — a restore from backup, a re-import, or
simply the same id reused — silently skips that message. That is a dropped
forward with no error anywhere.

``rule_stats`` even declares ``ON DELETE CASCADE`` in the schema, but the
connection runs with ``PRAGMA foreign_keys = 0``, so no cascade ever fires: the
cleanup has to be explicit and transactional in both stores.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import ForwardRule, MessagePayload, TelegramSession
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryQueueRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.durable_queue import DurableQueueManager
from unittest.mock import MagicMock

OWNER = 5094837833
OTHER = 5989354154
SOURCE = "-1004400615270"
TARGET = "-1003915313919"
RULE = "rule-keep"
DOOMED = "rule-doomed"


def make_rule(rule_id: str, owner: int = OWNER) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id, session_id=f"sess-{owner}", source_chat_id=SOURCE,
        target_chat_id=TARGET, is_active=True, owner_user_id=owner,
    )
    rule.duplicate_filters = False
    # One message, two tasks of one tenant: without this the single-match policy
    # gives the message to only the higher-priority task.
    rule.multi_route = True
    return rule


def seed(db) -> None:
    async def go():
        sessions = SqliteSessionRepository(db, CryptoService("test-key"))
        for owner in (OWNER, OTHER):
            await sessions.add(TelegramSession(
                id=f"sess-{owner}", phone_number="+100****0000", is_active=True,
                is_authorized=True, owner_user_id=owner,
            ))
        repo = SqliteForwardRuleRepository(db)
        await repo.add(make_rule(RULE))
        await repo.add(make_rule(DOOMED))

    asyncio.run(go())


def write_bookkeeping(db, rule_id: str, message_id: int = 900) -> None:
    """One row per dependent table, exactly the shape the engine writes."""
    now = int(time.time())
    db.execute(
        "INSERT INTO message_map (id, rule_id, source_chat_id, source_message_id,"
        " target_chat_id, target_message_id, delivery_status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (f"mm-{rule_id}", rule_id, SOURCE, message_id, TARGET, 4242, "SENT", now, now),
    )
    db.execute(
        "INSERT INTO processed_messages (rule_id, chat_id, message_id, ts)"
        " VALUES (?,?,?,?)",
        (rule_id, SOURCE, message_id, now),
    )
    db.execute(
        "INSERT INTO delivery_jobs (id, rule_id, source_chat_id, source_message_id,"
        " target_chat_id, payload_json, status, attempts, max_attempts, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"job-{rule_id}", rule_id, SOURCE, message_id, TARGET, "{}", "SENT", 1, 5, now, now),
    )
    db.execute(
        "INSERT INTO dead_letter_queue (id, job_id, rule_id, source_chat_id,"
        " source_message_id, target_chat_id, payload_json, attempts, last_error,"
        " error_category, dead_lettered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"dlq-{rule_id}", f"job-{rule_id}", rule_id, SOURCE, message_id, TARGET,
         "{}", 5, "boom", "DELIVERY", now),
    )
    db.execute(
        "INSERT INTO rule_stats (rule_id, forwarded, filtered, errors, last_forward_ts)"
        " VALUES (?,?,?,?,?)",
        (rule_id, 3, 1, 0, now),
    )
    db.execute(
        "INSERT INTO error_log (ts, rule_id, category, error_name, severity, detail, recoverable)"
        " VALUES (?,?,?,?,?,?,?)",
        (now, rule_id, "forward", "SomeError", "error", "detail", 1),
    )
    try:
        db.execute(
            "INSERT INTO content_dedup (rule_id, content_hash, created_at) VALUES (?,?,?)",
            (rule_id, f"hash-{rule_id}", now),
        )
    except Exception:
        pass


def counts(db, rule_id: str) -> dict:
    out = {}
    for table in SqliteForwardRuleRepository.DEPENDENT_TABLES:
        try:
            out[table] = db.query_one(
                f"SELECT COUNT(*) FROM {table} WHERE rule_id=?", (rule_id,))[0]
        except Exception as exc:  # pragma: no cover - schema drift
            out[table] = f"err:{exc}"
    return out


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path):
    database = SqliteDatabase(os.path.join(tmp_path, "delete.db"))
    try:
        seed(database)
        yield database
    finally:
        database.close()


# --------------------------------------------------------------------------- #
# The cleanup itself
# --------------------------------------------------------------------------- #
def test_deleting_a_task_removes_its_bookkeeping_and_nothing_else(db):
    write_bookkeeping(db, DOOMED, 901)
    write_bookkeeping(db, RULE, 902)
    kept_before = counts(db, RULE)

    assert run(SqliteForwardRuleRepository(db).delete(DOOMED)) is True

    assert run(SqliteForwardRuleRepository(db).get_by_id(DOOMED)) is None
    gone = counts(db, DOOMED)
    assert set(gone.values()) == {0}, f"a deleted task left rows behind: {gone}"

    # The other task is untouched, row for row: this is a per-task cleanup, not
    # a table sweep.
    assert counts(db, RULE) == kept_before, counts(db, RULE)
    assert run(SqliteForwardRuleRepository(db).get_by_id(RULE)) is not None


def test_deleting_a_task_that_is_not_there_changes_nothing(db):
    write_bookkeeping(db, RULE, 903)
    before = counts(db, RULE)

    assert run(SqliteForwardRuleRepository(db).delete("no-such-task")) is False
    assert counts(db, RULE) == before, "a no-op delete removed another task's rows"


def test_the_cleanup_is_one_transaction_not_a_sequence_of_commits(db):
    """Each `execute()` commits, so a half-done cleanup is a real state.

    The delete path must therefore go through the database's transaction helper
    instead of issuing the statements one by one.
    """
    import dis

    attributes = [
        instruction.argval
        for instruction in dis.get_instructions(SqliteForwardRuleRepository.delete)
        if instruction.opname in ("LOAD_ATTR", "LOAD_METHOD")
    ]
    assert "run_in_transaction" in attributes, attributes


# --------------------------------------------------------------------------- #
# The consequence a user would see
# --------------------------------------------------------------------------- #
def test_a_recreated_task_delivers_a_message_the_deleted_one_had_already_sent(db):
    queue = DurableQueueManager(
        queue_repo=SqliteDeliveryQueueRepository(db),
        message_map_repo=SqliteMessageMapRepository(db),
        client_pool=MagicMock(),
        rule_repo=SqliteForwardRuleRepository(db),
        num_workers=1,
    )
    pipeline = DurableMessagePipeline(
        rule_repo=SqliteForwardRuleRepository(db),
        filter_repo=SqliteFilterRuleRepository(db),
        queue_manager=queue,
        msg_map_repo=SqliteMessageMapRepository(db),
        processed_repo=SqliteProcessedMessageRepository(db),
    )
    payload = MessagePayload(message_id=910, chat_id=SOURCE, text="kept or skipped")

    run(pipeline.process(payload, trigger="NEW_MESSAGE"))
    first = db.query_all("SELECT id FROM delivery_jobs WHERE rule_id=?", (DOOMED,))
    assert len(first) == 1, first

    # The task is deleted and created again with the same id (restore, re-import,
    # id reuse). The dedupe records must not outlive the task that wrote them.
    assert run(SqliteForwardRuleRepository(db).delete(DOOMED)) is True
    run(SqliteForwardRuleRepository(db).add(make_rule(DOOMED)))

    run(pipeline.process(payload, trigger="NEW_MESSAGE"))
    again = db.query_all("SELECT id FROM delivery_jobs WHERE rule_id=?", (DOOMED,))
    assert len(again) == 1, (
        "the re-created task silently skipped a message its predecessor had "
        f"delivered: {again}"
    )


# --------------------------------------------------------------------------- #
# Both production stores, because a fix in one is a hole in the other
# --------------------------------------------------------------------------- #
def test_the_cleanup_covers_every_table_the_schema_gives_a_rule_id():
    """No dependent table may be forgotten when one is added later.

    The list is checked against the schema the database itself creates, so adding
    a ``rule_id`` column without adding the table here is a test failure rather
    than a new leak.
    """
    from core.infrastructure.persistence import database as db_module

    import re

    schema = "".join(db_module.MIGRATIONS[k] for k in sorted(db_module.MIGRATIONS))
    tables = set()
    for block in schema.split("CREATE TABLE IF NOT EXISTS ")[1:]:
        name = block.split("(")[0].strip()
        body = block.split(");")[0]
        # Exact column name, so `filter_rule_id`/`rule_id` substrings in other
        # column names do not drag a table into the list.
        if re.search(r"^\s*rule_id\s", body, re.MULTILINE):
            tables.add(name)

    assert {"message_map", "processed_messages", "delivery_jobs",
            "dead_letter_queue", "rule_stats", "error_log"} <= set(tables), tables
    missing = tables - set(SqliteForwardRuleRepository.DEPENDENT_TABLES)
    assert not missing, f"these tables carry a rule_id and are not cleaned up: {sorted(missing)}"


def test_the_postgres_delete_cleans_them_all_in_a_single_statement():
    """One statement, so there is no window where the rule is gone and the rows are not."""
    from core.infrastructure.persistence.postgres_repositories import (
        PostgresForwardRuleRepository,
    )

    class _FakeDb:
        def __init__(self):
            self.statements: list[str] = []

        async def fetchone(self, query, params=()):
            self.statements.append(query)
            return (1,)

        async def execute(self, query, params=()):
            self.statements.append(query)
            return "DELETE 1"

    fake = _FakeDb()
    repo = PostgresForwardRuleRepository(fake)
    assert run(repo.delete(DOOMED)) is True, "a deleted row must report True"

    assert len(fake.statements) == 1, f"the delete was not one atomic unit: {fake.statements}"
    statement = fake.statements[0]
    for table in ("forward_rules", "message_map", "delivery_jobs",
                  "dead_letter_queue", "rule_stats", "error_log"):
        assert f"DELETE FROM {table}" in statement, f"{table} is not cleaned: {statement}"
