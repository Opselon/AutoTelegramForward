"""The replace-rule manager: one rule across tasks, owner-scoped, previewable.

The manager is the single implementation every surface calls (the bot panel, the
dashboard, the mobile client and the gRPC layer). These tests drive it through
the real SQLite repositories and the real engine, so what they pin is what a
forwarded message will actually look like:

* a rule assigned to two tasks is **one** row, and editing it changes both
  copies (no per-task drift);
* enable/disable and priority are honoured by ``apply_replacements`` — a
  disabled rule leaves the text alone, a higher priority runs first;
* every read and write is owner-scoped: another tenant's id is "not found";
* a legacy ``"old": "new"`` entry is adopted, migrated on first edit, and never
  duplicated by the migration.
"""

from __future__ import annotations

import asyncio

import pytest

from core.application.replace_rules import ReplaceRuleError, ReplaceRuleService
from core.application.use_cases import ForwardRuleUseCases
from core.domain.entities import ForwardRule, TelegramSession
from core.domain.text_replace import (
    DEFAULT_PRIORITY,
    apply_replacements,
    entry_from_value,
    legacy_rule_id,
)
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteForwardRuleRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService

OWNER = 5094837833
OTHER = 6278059256
SOURCE = "-1004400615270"


def build(tmp_path):
    db = SqliteDatabase(str(tmp_path / "replace.db"))
    repo = SqliteForwardRuleRepository(db)
    service = ReplaceRuleService(ForwardRuleUseCases(repo))
    return db, repo, service


async def seed(db, repo, owner=OWNER, rules=(("t1", "Task One"), ("t2", "Task Two"))):
    session_id = "sess-a" if owner == OWNER else f"sess-{owner}"
    await SqliteSessionRepository(db, CryptoService("test-key")).add(
        TelegramSession(id=session_id, phone_number="+100****0000", is_active=True,
                        is_authorized=True, owner_user_id=owner)
    )
    out = []
    for index, (rid, name) in enumerate(rules, start=1):
        rule = ForwardRule(
            id=rid, session_id=session_id, name=name,
            source_chat_id=f"-1004400615{index:03d}", target_chat_id="-1003915313919",
            is_active=True, owner_user_id=owner,
        )
        await repo.add(rule)
        out.append(rule)
    return out


def run(coro_fn):
    """Run an async test body against a fresh database."""
    asyncio.run(coro_fn())


# --------------------------------------------------------------------------- #
# Engine: enabled + priority
# --------------------------------------------------------------------------- #


def test_disabled_entry_is_skipped_by_the_engine():
    mapping = {
        "promo": {"to": "OFFER", "enabled": False},
        "old": {"to": "new"},
    }
    assert apply_replacements("promo and old", mapping) == "promo and new"


def test_priority_decides_the_order_and_ties_keep_insertion_order():
    # A chain proves the order: `# -> ##` must run before `## -> z`.
    chain = {
        "r1": {"to": "##", "source": "#", "priority": 500},
        "r2": {"to": "z", "source": "##", "priority": 100},
    }
    assert apply_replacements("#", chain) == "z"
    # Reversing the priorities reverses the outcome.
    flipped = {
        "r1": {"to": "##", "source": "#", "priority": 1},
        "r2": {"to": "z", "source": "##", "priority": 2},
    }
    assert apply_replacements("#", flipped) == "##"
    # Equal priority keeps insertion order.
    ties = {
        "a": {"to": "b", "source": "a"},
        "b": {"to": "c", "source": "b"},
    }
    assert apply_replacements("a", ties) == "c"


def test_entry_round_trips_enabled_priority_and_id():
    entry = entry_from_value("k", {
        "id": "abc123", "source": "Gold", "to": "Silver",
        "enabled": False, "priority": 450, "mode": "regex", "flags": "i",
    })
    assert entry.rid == "abc123"
    assert entry.source == "Gold"
    assert entry.enabled is False
    assert entry.priority == 450
    stored = entry.to_rule_value("abc123")
    restored = entry_from_value("k", stored)
    assert restored == entry


# --------------------------------------------------------------------------- #
# Manager: create / list / update / delete / duplicate
# --------------------------------------------------------------------------- #


def test_create_assigns_one_rule_to_several_tasks(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        try:
            await seed(db, repo)
            rule = await service.create(OWNER, {
                "name": "Promo", "source": "promo", "to": "OFFER",
                "mode": "literal", "priority": 200, "task_ids": ["t1", "t2"],
            })
            assert rule.name == "Promo"
            assert sorted(rule.task_ids) == ["t1", "t2"]
            # One row in the manager...
            page = await service.list(OWNER)
            assert page["total"] == 1
            assert page["counts"] == {"total": 1, "enabled": 1, "disabled": 0}
            # ...and one copy inside each assigned task, keyed by the same id.
            for task_id in ("t1", "t2"):
                stored = await repo.get_owned(task_id, OWNER)
                assert rule.id in stored.replacements
                assert stored.replacements[rule.id]["source"] == "promo"
        finally:
            db.close()

    run(body)


def test_create_requires_a_task_and_refuses_a_foreign_one(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        try:
            await seed(db, repo)
            with pytest.raises(ReplaceRuleError) as err:
                await service.create(OWNER, {"source": "a", "to": "b"})
            assert err.value.code == "task_required"

            # A task that belongs to someone else answers like one that does not
            # exist: an id must not be usable to probe another tenant.
            with pytest.raises(ReplaceRuleError) as err:
                await service.create(OWNER, {"source": "a", "to": "b", "task_ids": ["nope"]})
            assert err.value.code == "task_not_found"

            # Another owner who does have tasks still cannot select ours.
            await seed(db, repo, owner=OTHER, rules=(("o1", "Other task"),))
            with pytest.raises(ReplaceRuleError) as err:
                await service.create(OTHER, {"source": "a", "to": "b", "task_ids": ["t1"]})
            assert err.value.code == "task_not_found"
        finally:
            db.close()

    run(body)


def test_list_searches_filters_and_paginates(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        try:
            await seed(db, repo)
            for i in range(24):
                await service.create(OWNER, {
                    "name": f"Rule {i:02d}", "source": f"word{i}", "to": f"out{i}",
                    "task_ids": ["t1"],
                })
            off_rule = await service.create(OWNER, {
                "name": "Switched off", "source": "off", "to": "x", "task_ids": ["t1"],
            })
            off_rule = await service.update(OWNER, off_rule.id, {"enabled": False})

            page1 = await service.list(OWNER, per_page=10, page=1)
            assert page1["total"] == 25 and page1["pages"] == 3
            assert len(page1["items"]) == 10
            assert page1["counts"] == {"total": 25, "enabled": 24, "disabled": 1}

            found = await service.list(OWNER, query="rule 07")
            assert found["total"] == 1 and found["items"][0]["name"] == "Rule 07"

            off = await service.list(OWNER, status="disabled")
            assert off["total"] == 1 and off["items"][0]["id"] == off_rule.id

            last = await service.list(OWNER, per_page=10, page=3)
            assert len(last["items"]) == 5
            # A page past the end is clamped, not an error (a list can shrink
            # between the render and the click).
            assert (await service.list(OWNER, per_page=10, page=99))["page"] == 3

            # The task filter narrows the set and the counts with it.
            scoped = await service.list(OWNER, task_id="t2")
            assert scoped["counts"]["total"] == 0
        finally:
            db.close()

    run(body)


def test_update_reassigns_tasks_without_leaving_stale_copies(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        try:
            await seed(db, repo)
            rule = await service.create(OWNER, {
                "name": "Phone", "source": "0912", "to": "+98", "task_ids": ["t1", "t2"],
            })
            updated = await service.update(OWNER, rule.id, {
                "name": "Phone v2", "task_ids": ["t1"],
            })
            assert updated.task_ids == ("t1",)
            assert updated.name == "Phone v2"
            # t2 no longer runs it; t1 does.
            assert rule.id not in (await repo.get_owned("t2", OWNER)).replacements
            assert rule.id in (await repo.get_owned("t1", OWNER)).replacements
            # The rule is still one row.
            assert (await service.list(OWNER))["total"] == 1
        finally:
            db.close()

    run(body)


def test_toggle_flips_every_copy(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        try:
            await seed(db, repo)
            rule = await service.create(OWNER, {
                "source": "a", "to": "b", "task_ids": ["t1", "t2"],
            })
            off = await service.toggle(OWNER, rule.id)
            assert off.enabled is False
            for task_id in ("t1", "t2"):
                stored = (await repo.get_owned(task_id, OWNER)).replacements[rule.id]
                assert stored["enabled"] is False
            on = await service.toggle(OWNER, rule.id)
            assert on.enabled is True
        finally:
            db.close()

    run(body)


def test_duplicate_copies_the_payload_to_the_same_tasks(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        try:
            await seed(db, repo)
            rule = await service.create(OWNER, {
                "name": "Regex", "source": r"promo\s*(\d+)", "to": "OFFER",
                "mode": "regex", "flags": "i", "priority": 300, "task_ids": ["t1"],
            })
            clone = await service.duplicate(OWNER, rule.id, name="Regex copy")
            assert clone.id != rule.id
            assert clone.name == "Regex copy"
            assert clone.mode == "regex" and clone.priority == 300
            assert clone.task_ids == rule.task_ids
            assert (await service.list(OWNER))["total"] == 2
        finally:
            db.close()

    run(body)


def test_delete_removes_every_copy(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        try:
            await seed(db, repo)
            rule = await service.create(OWNER, {"source": "a", "to": "b", "task_ids": ["t1", "t2"]})
            result = await service.delete(OWNER, rule.id)
            assert result["removed"] == 2
            assert (await service.list(OWNER))["total"] == 0
            with pytest.raises(ReplaceRuleError) as err:
                await service.delete(OWNER, rule.id)
            assert err.value.code == "not_found"
        finally:
            db.close()

    run(body)


def test_another_tenant_cannot_read_or_write_the_rule(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        try:
            await seed(db, repo)
            await seed(db, repo, owner=OTHER, rules=(("o1", "Other task"),))
            rule = await service.create(OWNER, {"source": "a", "to": "b", "task_ids": ["t1"]})
            with pytest.raises(ReplaceRuleError) as err:
                await service.get(OTHER, rule.id)
            assert err.value.code == "not_found"
            with pytest.raises(ReplaceRuleError):
                await service.update(OTHER, rule.id, {"to": "hijack"})
            with pytest.raises(ReplaceRuleError):
                await service.delete(OTHER, rule.id)
            # And the other tenant's list is empty of it.
            assert (await service.list(OTHER))["total"] == 0
            # The owner's copies are untouched.
            stored = (await repo.get_owned("t1", OWNER)).replacements[rule.id]
            assert stored["to"] == "b"
        finally:
            db.close()

    run(body)


# --------------------------------------------------------------------------- #
# Legacy entries
# --------------------------------------------------------------------------- #


def test_legacy_entry_is_adopted_and_migrated_on_first_edit(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        try:
            await seed(db, repo)
            task = await repo.get_owned("t1", OWNER)
            task.replacements = {"binance": "exchange"}
            await repo.update(task)

            page = await service.list(OWNER)
            assert page["total"] == 1
            legacy = page["items"][0]
            assert legacy["source"] == "binance"
            assert legacy["id"] == legacy_rule_id("t1", "binance")
            assert legacy["enabled"] is True

            # Editing migrates the entry to the id-keyed shape, in place.
            updated = await service.update(OWNER, legacy["id"], {"to": "BINANCE"})
            stored = (await repo.get_owned("t1", OWNER)).replacements
            assert stored[legacy["id"]]["source"] == "binance"
            assert stored[legacy["id"]]["to"] == "BINANCE"
            assert "binance" not in stored  # no duplicate left behind
            assert updated.id == legacy["id"]
            assert len(stored) == 1
        finally:
            db.close()

    run(body)


# --------------------------------------------------------------------------- #
# Preview (test)
# --------------------------------------------------------------------------- #


def test_test_preview_uses_the_same_engine(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        try:
            await seed(db, repo)
            out = await service.test(OWNER, {
                "source": r"promo\s*(\d+)", "to": r"OFFER \1",
                "mode": "regex", "flags": "i", "sample": "PROMO 50 and promo 20",
            })
            assert out["output"] == "OFFER 50 and OFFER 20"
            assert out["matches"] == 2 and out["changed"] is True

            miss = await service.test(OWNER, {
                "source": "absent", "to": "x", "sample": "nothing here",
            })
            assert miss["output"] == "nothing here"
            assert miss["matches"] == 0 and miss["changed"] is False
        finally:
            db.close()

    run(body)


def test_invalid_regex_is_refused_at_write_time(tmp_path):
    async def body():
        db, repo, service = build(tmp_path)
        try:
            await seed(db, repo)
            with pytest.raises(ReplaceRuleError) as err:
                await service.create(OWNER, {
                    "source": "a(b", "to": "x", "mode": "regex", "task_ids": ["t1"],
                })
            assert err.value.code == "invalid_entry"
            assert (await service.list(OWNER))["total"] == 0
        finally:
            db.close()

    run(body)


def test_default_priority_is_the_documented_one():
    entry = entry_from_value("a", "b")
    assert entry.priority == DEFAULT_PRIORITY == 100
    assert entry.enabled is True
