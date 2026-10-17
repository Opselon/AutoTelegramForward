"""The reads a message and a task list hit on every call stay indexed.

These are the guards for the two scale defects found by measuring at 10,000
tenants — both invisible in a unit test with three rows, because SQLite will
happily scan a table that small:

  * ``list_active_by_source`` was a **full scan of forward_rules**: no index can
    answer ``source_chat_id = ? OR EXISTS(json_each(...))``, so every inbound
    message read every rule in the system;
  * the owner-scoped page sorted through a temp B-tree.

A plan assertion is the only kind of test that catches this class of defect, so
the tests below ask the planner directly. They also pin the behaviour the
rewritten query must keep: extra sources still match, nothing matches twice, and
a rule with unparseable criteria does not break the query for everyone.
"""
from __future__ import annotations

import pytest

from core.domain.entities import ForwardRule
from core.domain.value_objects import ForwardMode, RoutingType
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import SqliteForwardRuleRepository

OWNER = 5094837833


class Env:
    def __init__(self):
        self.db = SqliteDatabase(":memory:")
        self.rules = SqliteForwardRuleRepository(self.db)
        self.db.execute(
            "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
            " username, first_name, is_active, is_authorized, created_at, updated_at)"
            " VALUES ('s1', '+1000', '', '', '', '', 1, 1, 0, 0)", (),
        )

    async def add(self, rule_id: str, source: str, extra=None, owner: int = OWNER,
                  active: bool = True, criteria_raw: str | None = None):
        rule = ForwardRule(
            id=rule_id, name=rule_id, session_id="s1", source_chat_id=source,
            target_chat_id="-1002", routing_type=RoutingType.CHANNEL_TO_CHANNEL,
            forward_mode=ForwardMode.COPY_MESSAGE, owner_user_id=owner,
        )
        rule.is_active = active
        if extra:
            rule.detection_criteria = {"source_chat_ids": extra}
        await self.rules.add(rule)
        if criteria_raw is not None:
            # Only reachable by hand-editing or an older build: exercise it, since
            # a single unparseable document must not break the whole query.
            self.db.execute(
                "UPDATE forward_rules SET detection_criteria=? WHERE id=?",
                (criteria_raw, rule_id),
            )
        return rule

    def plan(self, sql: str, params: tuple) -> str:
        rows = self.db.query_all(f"EXPLAIN QUERY PLAN {sql}", params)
        return " | ".join(
            str(r["detail"] if isinstance(r, dict) else r[3]) for r in rows
        )


@pytest.fixture()
def env():
    return Env()


def _val(row, key: str, index: int = 1):
    """Read a column out of whatever row shape the driver returns."""
    try:
        return row[key]
    except (TypeError, IndexError, KeyError):
        return row[index]


def _source_sql() -> str:
    """The repository's own statement, read out of its source.

    Deliberately not a copy: a copy would keep passing after the query changed
    back to something unindexable. This asks the implementation what it runs.
    """
    import inspect
    source = inspect.getsource(SqliteForwardRuleRepository.list_active_by_source)
    sql = source.split('query_all(', 1)[1]
    pieces = []
    for chunk in sql.split('"')[1::2]:
        pieces.append(chunk)
    return "".join(pieces).strip()


# --------------------------------------------------------------------------- #
# The plans
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_per_message_lookup_does_not_scan_the_rules_table(env):
    for i in range(50):
        await env.add(f"r{i}", f"-100{i}", extra=[f"-200{i}"] if i % 10 == 0 else None)
    plan = env.plan(_source_sql(), ("-1001", "-1001", "-1001"))
    assert "SCAN forward_rules" not in plan, (
        f"the per-message lookup is a full table scan again: {plan}"
    )
    assert "idx_rules_source" in plan
    assert "idx_rules_extra_sources" in plan, (
        "the extra-source branch is no longer index-guarded; with many tasks that "
        "branch becomes a table scan"
    )


@pytest.mark.asyncio
async def test_the_owner_page_does_not_sort_through_a_temp_btree(env):
    for i in range(20):
        await env.add(f"r{i}", f"-100{i}")
    sql = ("SELECT * FROM forward_rules WHERE owner_user_id=? ORDER BY created_at"
           " LIMIT 50 OFFSET 0")
    plan = env.plan(sql, (OWNER,))
    assert "TEMP B-TREE" not in plan, f"the page is sorted in memory again: {plan}"
    assert "idx_rules_owner_created" in plan


@pytest.mark.asyncio
async def test_the_indexes_the_plan_assumes_exist(env):
    names = {
        str(_val(r, "name")) for r in env.db.query_all(
            "SELECT name FROM sqlite_master WHERE type='index'", ()
        )
    }
    assert "idx_rules_extra_sources" in names
    assert "idx_rules_owner_created" in names
    # Subsumed by the composite index: keeping both would only cost writes.
    assert "idx_rules_owner" not in names


# --------------------------------------------------------------------------- #
# The behaviour the rewrite must keep
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_primary_source_matches(env):
    await env.add("r1", "-1001")
    found = await env.rules.list_active_by_source("-1001")
    assert [r.id for r in found] == ["r1"]


@pytest.mark.asyncio
async def test_an_extra_source_matches(env):
    await env.add("r1", "-1001", extra=["-2001"])
    assert [r.id for r in await env.rules.list_active_by_source("-2001")] == ["r1"]


@pytest.mark.asyncio
async def test_a_rule_is_never_returned_twice(env):
    """It watches the chat as both its primary and its extra source."""
    await env.add("r1", "-1001", extra=["-1001"])
    found = await env.rules.list_active_by_source("-1001")
    assert [r.id for r in found] == ["r1"], "the rule was returned by both branches"


@pytest.mark.asyncio
async def test_extra_source_matching_still_works_for_a_different_chat(env):
    await env.add("r1", "-1001", extra=["-2001"])
    await env.add("r2", "-1001", extra=["-2002"])
    found = await env.rules.list_active_by_source("-1001")
    assert sorted(r.id for r in found) == ["r1", "r2"]
    assert sorted(r.id for r in await env.rules.list_active_by_source("-2002")) == ["r2"]


@pytest.mark.asyncio
async def test_an_inactive_rule_never_matches(env):
    await env.add("r1", "-1001", extra=["-2001"], active=False)
    assert await env.rules.list_active_by_source("-1001") == []
    assert await env.rules.list_active_by_source("-2001") == []


@pytest.mark.asyncio
async def test_unparseable_criteria_do_not_break_the_query(env):
    """One bad document must not stop forwarding for every other rule."""
    await env.add("bad", "-1009", criteria_raw="not json at all")
    await env.add("good", "-1001")
    assert [r.id for r in await env.rules.list_active_by_source("-1001")] == ["good"]


@pytest.mark.asyncio
async def test_an_empty_extra_source_list_is_not_a_match(env):
    await env.add("r1", "-1001", criteria_raw='{"source_chat_ids": []}')
    assert await env.rules.list_active_by_source("-2001") == []


@pytest.mark.asyncio
async def test_the_flag_is_derived_not_writable(env):
    """Nothing may write it, or it could disagree with the JSON it comes from."""
    with pytest.raises(Exception):
        env.db.execute(
            "UPDATE forward_rules SET has_extra_sources=1 WHERE id='r1'", ()
        )


@pytest.mark.asyncio
async def test_changing_the_criteria_changes_matching_immediately(env):
    """No cache and no backfill: the flag is recomputed by the engine."""
    rule = await env.add("r1", "-1001")
    assert await env.rules.list_active_by_source("-2001") == []
    rule.detection_criteria = {"source_chat_ids": ["-2001"]}
    await env.rules.update(rule)
    assert [r.id for r in await env.rules.list_active_by_source("-2001")] == ["r1"]
    rule.detection_criteria = {}
    await env.rules.update(rule)
    assert await env.rules.list_active_by_source("-2001") == []


# --------------------------------------------------------------------------- #
# Paging
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_an_owner_page_is_bounded_and_ordered(env):
    for i in range(30):
        await env.add(f"r{i:02d}", f"-100{i}")
    first = await env.rules.list_by_owner(OWNER, limit=10)
    second = await env.rules.list_by_owner(OWNER, limit=10, offset=10)
    assert len(first) == 10 and len(second) == 10
    assert {r.id for r in first}.isdisjoint({r.id for r in second})
    assert first[0].created_at <= first[-1].created_at
    assert await env.rules.count_by_owner(OWNER) == 30


@pytest.mark.asyncio
async def test_an_owner_never_sees_another_owners_tasks(env):
    await env.add("mine", "-1001", owner=OWNER)
    await env.add("theirs", "-1002", owner=OWNER + 1)
    assert [r.id for r in await env.rules.list_by_owner(OWNER)] == ["mine"]
    assert await env.rules.count_by_owner(OWNER) == 1
    assert await env.rules.list_by_owner(OWNER, limit=10, offset=0) != []


@pytest.mark.asyncio
async def test_paging_past_the_end_is_empty_not_an_error(env):
    await env.add("r1", "-1001")
    assert await env.rules.list_by_owner(OWNER, limit=10, offset=500) == []


@pytest.mark.asyncio
async def test_the_default_is_still_everything(env):
    """Existing callers keep the behaviour they had."""
    for i in range(5):
        await env.add(f"r{i}", f"-100{i}")
    assert len(await env.rules.list_by_owner(OWNER)) == 5


# --------------------------------------------------------------------------- #
# The upgrade
# --------------------------------------------------------------------------- #
def test_an_existing_database_picks_the_indexes_up(tmp_path):
    """Opening a database created before v21 must add them, not fail."""
    path = str(tmp_path / "old.db")
    first = SqliteDatabase(path)
    first.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
        " username, first_name, is_active, is_authorized, created_at, updated_at)"
        " VALUES ('s1', '+1000', '', '', '', '', 1, 1, 0, 0)", (),
    )
    first.execute(
        "INSERT INTO forward_rules (id, session_id, source_chat_id, target_chat_id,"
        " routing_type, forward_mode, created_at, updated_at)"
        " VALUES ('r1', 's1', '-1001', '-1002', 'CHANNEL_TO_CHANNEL', 'COPY_MESSAGE', 0, 0)",
        (),
    )
    first.close()

    reopened = SqliteDatabase(path)
    names = {
        str(_val(r, "name"))
        for r in reopened.query_all("SELECT name FROM sqlite_master WHERE type='index'", ())
    }
    assert "idx_rules_extra_sources" in names
    assert "idx_rules_owner_created" in names
    # The row that survived the upgrade still matches by its primary source.
    plan = reopened.query_all(
        "EXPLAIN QUERY PLAN SELECT * FROM forward_rules WHERE is_active=1"
        " AND source_chat_id = ?", ("-1001",),
    )
    detail = " | ".join(
        str(r["detail"] if isinstance(r, dict) else r[3]) for r in plan
    )
    assert "idx_rules_source" in detail
    reopened.close()


def test_opening_twice_is_harmless(tmp_path):
    """The index install must be idempotent — it runs on every start."""
    path = str(tmp_path / "twice.db")
    for _ in range(3):
        db = SqliteDatabase(path)
        db.close()
    db = SqliteDatabase(path)
    count = db.query_one(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='index'"
        " AND name='idx_rules_extra_sources'", ()
    )
    assert int(_val(count, "n", 0)) == 1
    db.close()
