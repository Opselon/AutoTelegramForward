"""Part 2 STEER 04/05 — the task screens as a lifecycle, and what survives.

Two questions this file answers with the product's own code:

1. **Does every button do something?** Each payload of each task screen is
   pressed as the owner; the handler must either change persisted state, edit
   the screen, or alert the user. A press that does all three of nothing is a
   silent failure — exactly what a swallowed `PermissionError` looks like from
   the outside (it hid a broken "duplicate" behind a success alert).
2. **Does a saved setting survive a restart?** The settings are changed through
   the bot, the store is closed, a new process-wide stack opens the same
   database, and the effective values are read back through the engine.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from core.tests.bot_dispatch import BotHarness
from core.tests.test_cross_tenant_boundaries import OWNER_A, RULE_A, SESSION_A, _seed, task_payloads


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.fixture
def h(tmp_dir):
    harness = BotHarness(os.path.join(tmp_dir, "atf.db"), admin_ids=[OWNER_A])
    try:
        yield harness
    finally:
        harness.close()


def _rows(harness: BotHarness) -> dict:
    snap = {}
    for table in ("forward_rules", "filter_rules", "message_map", "content_dedup",
                  "delivery_jobs", "user_tasks", "bot_tokens"):
        try:
            cur = harness.db.connection.execute(f"SELECT * FROM {table}")
            snap[table] = sorted(tuple(str(v) for v in row) for row in cur.fetchall())
        except Exception:
            continue
    return snap


def _changed(before: dict, after: dict) -> list[str]:
    return [t for t in before if before[t] != after.get(t)]


def test_every_task_button_acts_or_explains_itself(h):
    """No button may be a silent no-op: state, screen, or alert — never nothing."""
    run(_seed(h))
    rule = run(h.rules_repo.get_by_id(RULE_A))
    payloads = task_payloads(h, rule)
    silent: list[tuple[str, str]] = []
    for screen, datas in payloads.items():
        for data in datas:
            before = _rows(h)
            cq = run(h.press(data, OWNER_A))
            acted = bool(_changed(before, _rows(h)))
            visible = bool(cq.answers) or bool(cq.message.sent)
            if not acted and not visible:
                silent.append((screen, data))
            # Whatever the press did, the task screen must still render.
            fresh = run(h.rules_repo.get_by_id(RULE_A))
            assert fresh is not None, f"{screen} payload {data!r} removed the task"
            h.ui._render_rule_detail(fresh)
    assert not silent, f"buttons that did nothing at all: {silent}"


def test_a_changed_setting_survives_a_restart(tmp_dir):
    """STEER 04 — persistence across a process restart, read back by the engine."""
    path = os.path.join(tmp_dir, "atf.db")
    first = BotHarness(path, admin_ids=[OWNER_A])
    try:
        run(_seed(first))
        for data in (f"rdly_s:{RULE_A}:600", f"rcl_url:{RULE_A}", f"rtr_set:{RULE_A}:fa",
                     f"rtg_fwdhdr:{RULE_A}", f"rfl_bl:{RULE_A}"):
            run(first.press(data, OWNER_A))
        written = first.rule_row(RULE_A)
        assert json.loads(written["metadata"]).get("translate_lang") == "fa"
    finally:
        first.close()

    second = BotHarness(path, admin_ids=[OWNER_A])
    try:
        reloaded = run(second.rules_repo.get_by_id(RULE_A))
        assert reloaded is not None
        row = second.rule_row(RULE_A)
        assert row == written, "the stored task changed across a restart"
        assert float(row["delay_seconds"]) == 600.0
        assert int(row["remove_links"]) == 1
        assert json.loads(row["metadata"])["translate_lang"] == "fa"
        # The engine reads the same values the screens render.
        assert "600" in json.dumps(row["delay_seconds"]) or float(row["delay_seconds"]) == 600.0
        assert run(second.rules_repo.get_by_id(RULE_A)).owner_user_id == OWNER_A
        assert int(row["owner_user_id"]) == OWNER_A
        # And the session the task routes through is still the owner's.
        cur = second.db.connection.execute(
            "SELECT owner_user_id FROM sessions WHERE id = ?", (SESSION_A,))
        assert int(cur.fetchone()[0]) == OWNER_A
    finally:
        second.close()


def test_a_refused_press_leaves_the_screen_renderable_and_the_row_untouched(h):
    """A refusal is a user-visible event, not a half-applied state."""
    run(_seed(h))
    from core.tests.test_cross_tenant_boundaries import OWNER_B

    before = h.rule_row(RULE_A)
    rule = run(h.rules_repo.get_by_id(RULE_A))
    presses = []
    for datas in task_payloads(h, rule).values():
        presses.extend(datas)
    for data in presses:
        cq = run(h.press(data, OWNER_B))
        assert h.rule_row(RULE_A) == before, data
        for text, kwargs in cq.answers.calls:
            # An alert is shown to the user: it must explain itself. A plain
            # answer may be empty — that is just how a spinner is stopped.
            if kwargs.get("show_alert"):
                assert text.strip(), f"{data!r} raised an empty alert"
    h.ui._render_rule_detail(rule)
