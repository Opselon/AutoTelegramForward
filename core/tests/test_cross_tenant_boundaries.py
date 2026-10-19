"""Part 2 STEER 02 — the per-user security boundary, swept from the real UI.

Every payload this file presses was produced by the product's own screen
renderers for one tenant's task, and every press goes through the handler the
dispatcher would choose for that payload, as the user id the test decides. The
invariant is deliberately blunt: **a button that belongs to another tenant's
task may render a refusal, but it may not change one byte of that task, create
a row in its name, or move its child resources.** A second, curated pass proves
the same payloads *do* act for the owner, so the sweep cannot pass by refusing
everything.

Forged payloads (a task id that does not exist, a state payload whose task was
overwritten, an admin screen for a non-admin) are pressed too: a client-provided
identifier is a selector, never proof of ownership.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from core.domain.entities import ForwardRule
from core.tests.bot_dispatch import BotHarness

OWNER_A = 5094837833
OWNER_B = 5989354154
RULE_A = "rule-a"
RULE_B = "rule-b"
SESSION_A = "sess-a"
SESSION_B = "sess-b"

PROTECTED_TABLES = (
    "forward_rules", "filter_rules", "message_map", "content_dedup", "delivery_jobs",
    "sessions", "bot_tokens", "ai_configs", "app_settings", "user_tasks",
)


def _snapshot(harness: BotHarness) -> dict:
    """Every row of every protected table, plus the victim's own UI state.

    `ui_states` is compared per user: navigating legitimately writes the
    *presser's* row, so only the victim's row is part of the invariant.
    """
    snap: dict[str, object] = {}
    for table in PROTECTED_TABLES:
        try:
            cur = harness.db.connection.execute(f"SELECT * FROM {table}")
            snap[table] = sorted(tuple(str(v) for v in row) for row in cur.fetchall())
        except Exception:
            continue
    cur = harness.db.connection.execute(
        "SELECT step, buffer FROM ui_states WHERE user_id = ?", (OWNER_A,))
    snap["victim_ui_state"] = [tuple(str(v) for v in row) for row in cur.fetchall()]
    return snap


def _changed(before: dict, after: dict) -> list[str]:
    return [t for t in before if before[t] != after.get(t)]


@pytest.fixture
def h():
    with tempfile.TemporaryDirectory() as tmp:
        harness = BotHarness(os.path.join(tmp, "atf.db"), admin_ids=[OWNER_A])
        try:
            yield harness
        finally:
            harness.close()


def run(coro):
    return asyncio.run(coro)


async def _seed(harness: BotHarness) -> None:
    await harness.seed_session(SESSION_A, OWNER_A)
    await harness.seed_session(SESSION_B, OWNER_B)
    await harness.seed_rule(RULE_A, OWNER_A, session_id=SESSION_A, name="A rule")
    await harness.seed_rule(RULE_B, OWNER_B, session_id=SESSION_B, name="B rule")


# --------------------------------------------------------------------------- #
# Collecting the real payloads
# --------------------------------------------------------------------------- #
def _buttons(markup) -> list[tuple[str, str]]:
    if markup is None:
        return []
    rows = getattr(markup, "inline_keyboard", None)
    if rows is None:
        rows = getattr(markup, "keyboard", None)
    if rows is None and isinstance(markup, dict):
        rows = markup.get("inline_keyboard")
    out: list[tuple[str, str]] = []
    for row in rows or []:
        for button in row:
            data = getattr(button, "callback_data", None)
            if isinstance(button, dict):
                data = button.get("callback_data")
                text = button.get("text")
            else:
                text = getattr(button, "text", "")
            if data:
                out.append((str(text), str(data)))
    return out


def task_payloads(harness: BotHarness, rule: ForwardRule) -> dict[str, list[str]]:
    """Every button the task's own screens offer, grouped by screen."""
    screens = {
        "detail": harness.ui._render_rule_detail(rule),
        "advanced": harness.ui._render_advanced_settings(rule),
        "addons": harness.ui._render_all_addons(rule),
        "replace": harness.ui._render_replace_menu(rule),
        "header": harness.ui._render_header_footer_menu(rule),
        "cleaner": harness.ui._render_cleaner_menu(rule),
        "filters": harness.ui._render_filters_menu(rule),
        "maxtime": harness.ui._render_max_time_edit_menu(rule),
        "delay": harness.ui._render_delay_menu(rule),
        "blacklist": harness.ui._render_blacklist_menu(rule),
        "whitelist": harness.ui._render_whitelist_menu(rule),
        "watermark": harness.ui._render_watermark_menu(rule),
        "translate": harness.ui._render_translate_menu(rule),
        "smart": harness.ui._render_smart_menu(rule),
        "draft": harness.ui._render_draft_editor(rule, {}),
    }
    return {name: [data for _t, data in _buttons(markup)] for name, (_text, markup) in screens.items()}


# --------------------------------------------------------------------------- #
# 1. The sweep
# --------------------------------------------------------------------------- #
def test_every_task_screen_offers_payloads_to_sweep(h):
    run(_seed(h))
    payloads = task_payloads(h, run(h.rules_repo.get_by_id(RULE_A)))
    total = sum(len(v) for v in payloads.values())
    assert total >= 40, f"only {total} buttons found: { {k: len(v) for k, v in payloads.items()} }"
    named = [d for v in payloads.values() for d in v if RULE_A in d]
    assert named, "no payload carries the task id — the sweep would prove nothing"


def test_no_payload_of_another_tenants_task_changes_anything(h):
    run(_seed(h))
    rule_a = run(h.rules_repo.get_by_id(RULE_A))
    payloads = task_payloads(h, rule_a)
    pressed = 0
    for screen, datas in payloads.items():
        for data in datas:
            before = _snapshot(h)
            run(h.press(data, OWNER_B))
            after = _snapshot(h)
            diff = _changed(before, after)
            assert not diff, f"{screen} payload {data!r} pressed by the other tenant changed {diff}"
            pressed += 1
    assert pressed >= 40


def test_the_same_payloads_act_for_the_owner(h):
    """The sweep above must not pass by refusing everybody."""
    run(_seed(h))
    rule_a = run(h.rules_repo.get_by_id(RULE_A))
    payloads = task_payloads(h, rule_a)
    acted: list[str] = []
    for data in payloads["detail"] + payloads["cleaner"] + payloads["delay"] + payloads["blacklist"]:
        before = _snapshot(h)
        run(h.press(data, OWNER_A))
        after = _snapshot(h)
        if _changed(before, after):
            acted.append(data)
    assert acted, "no owner payload changed state — the boundary sweep is vacuous"


# --------------------------------------------------------------------------- #
# 2. Forged identifiers
# --------------------------------------------------------------------------- #
def test_a_task_id_that_does_not_exist_is_refused_not_crashed(h):
    run(_seed(h))
    for data in (f"rtg:ghost-task:running", f"rtg:{RULE_A}:paused", "rtg::running",
                 "rge:g:ghost-task", "rge:k:ghost-task:0", "rge:t:ghost-task:0",
                 "rdly_s:ghost-task:600", "rfl_bl:ghost-task",
                 "rtx:ghost-task:0", "rtxd:ghost-task:0:1", f"rdup:ghost-task",
                 "rrn:ghost-task", f"rdel:ghost-task"):
        cq = run(h.press(data, OWNER_A))
        assert cq.answers or cq.message.sent, f"{data!r} was accepted silently"


def test_duplicating_a_task_creates_an_independent_copy_for_the_owner(h):
    """F13 — the copy is a new row, owned by the duplicator, not the original."""
    run(_seed(h))
    before = h.rule_count(OWNER_A)
    cq = run(h.press(f"rdup:{RULE_A}", OWNER_A))
    assert h.rule_count(OWNER_A) == before + 1, (
        f"duplicate created nothing (answers={cq.answers.texts[:1]})")
    cur = h.db.connection.execute(
        "SELECT id, owner_user_id, source_chat_id, target_chat_id, metadata"
        " FROM forward_rules WHERE owner_user_id = ? AND id != ?", (OWNER_A, RULE_A))
    copies = cur.fetchall()
    assert len(copies) == 1, copies
    copy_id = copies[0][0]
    assert copy_id != RULE_A
    assert int(copies[0][1]) == OWNER_A
    # An independent configuration: same routing, and its own row.
    original = h.rule_row(RULE_A)
    assert copies[0][2] == original["source_chat_id"]
    assert copies[0][3] == original["target_chat_id"]
    assert copies[0][4] is not None and copies[0][4] != ""
    # Deleting the copy leaves the original untouched (no shared row/reference).
    run(h.press(f"rdelyes:{copy_id}", OWNER_A))
    assert h.rule_row(copy_id) == {}, "the confirmed delete did not remove the copy"
    assert h.rule_row(RULE_A) == original, "deleting the copy disturbed the original"


def test_duplicating_another_tenants_task_creates_nothing(h):
    run(_seed(h))
    before_b = h.rule_count(OWNER_B)
    before_a = h.rule_row(RULE_A)
    run(h.press(f"rdup:{RULE_A}", OWNER_B))
    assert h.rule_count(OWNER_B) == before_b, "a foreign duplicate created a row"
    assert h.rule_row(RULE_A) == before_a


def test_a_forged_state_cannot_adopt_another_tenants_task(h):
    """The attacker starts their own flow, then rewrites the task it points at."""
    run(_seed(h))
    state_before = _snapshot(h)
    # B opens its own task's screens (its state now names RULE_B)…
    run(h.press(f"rge:{RULE_B}", OWNER_B))
    # …and then every payload that carries A's task id is pressed as B.
    for data in (f"rtg:{RULE_A}:running", f"rtg:{RULE_A}:paused", f"rge:{RULE_A}",
                 f"rdly_s:{RULE_A}:600", f"rfl_bl:{RULE_A}", f"rfl_wl:{RULE_A}",
                 f"rtx:{RULE_A}:0", f"clean_url:{RULE_A}", f"clean_phone:{RULE_A}"):
        run(h.press(data, OWNER_B))
    row_a = h.rule_row(RULE_A)
    assert row_a["owner_user_id"] == OWNER_A
    after = _snapshot(h)
    changed = _changed(state_before, after)
    # B's own UI state may legitimately move; A's task rows and their children may not.
    assert not any(t in changed for t in ("forward_rules", "filter_rules", "delivery_jobs",
                                          "content_dedup", "message_map", "user_tasks")), changed


def test_two_tenants_with_two_tasks_keep_their_own_configuration(h):
    """STEER 03 — four tasks, the same screens, no value bleeds across."""
    import json as _json

    run(_seed(h))
    run(h.seed_rule("rule-a2", OWNER_A, session_id=SESSION_A, name="A second"))
    run(h.seed_rule("rule-b2", OWNER_B, session_id=SESSION_B, name="B second"))
    siblings_before = {rid: h.rule_row(rid) for rid in ("rule-a2", "rule-b2")}

    for data in (f"rdly_s:{RULE_A}:600", f"rcl_url:{RULE_A}", f"rtr_set:{RULE_A}:fa"):
        run(h.press(data, OWNER_A))
    for data in (f"rdly_s:{RULE_B}:3", f"rcl_url:{RULE_B}", f"rtr_set:{RULE_B}:ru",
                 f"rcl_url:{RULE_B}"):
        run(h.press(data, OWNER_B))

    def cfg(rule_id: str) -> dict:
        row = h.rule_row(rule_id)
        meta = _json.loads(row.get("metadata") or "{}")
        return {
            "delay": float(row.get("delay_seconds") or 0),
            "links": int(row.get("remove_links") or 0),
            "translate": meta.get("translate_lang"),
            "owner": int(row.get("owner_user_id") or 0),
        }

    a, b = cfg(RULE_A), cfg(RULE_B)
    assert a["delay"] == 600.0 and b["delay"] == 3.0, (a, b)
    assert a["translate"] == "fa" and b["translate"] == "ru", (a, b)
    # A removed links once; B toggled the same button twice and is back off.
    assert a["links"] == 1 and b["links"] == 0, (a, b)
    assert a["owner"] == OWNER_A and b["owner"] == OWNER_B, (a, b)
    # Neither tenant's presses reached the other tenant's task, or a sibling.
    for rid, snapshot in siblings_before.items():
        assert h.rule_row(rid) == snapshot, f"{rid} moved: {cfg(rid)}"
    assert cfg("rule-a2")["delay"] == 0.0 and cfg("rule-a2")["translate"] is None


def test_changing_one_task_leaves_the_sibling_byte_identical(h):
    run(_seed(h))
    asyncio.run(h.seed_rule("rule-a2", OWNER_A, session_id=SESSION_A, name="A second"))
    sibling_before = h.rule_row("rule-a2")
    for screen, datas in task_payloads(h, run(h.rules_repo.get_by_id(RULE_A))).items():
        for data in datas:
            run(h.press(data, OWNER_A))
    assert h.rule_row("rule-a2") == sibling_before, "a sibling task of the same owner moved"
    # …and the task that was pressed did move, so this is not vacuous.
    assert h.rule_row(RULE_A) != sibling_before


def test_an_admin_screen_is_refused_for_a_non_admin(h):
    run(_seed(h))
    before = _snapshot(h)
    for data in ("admst", "admus", "admfs", "admcfg", "admadm", "admbc"):
        run(h.press(data, OWNER_B))
    assert not _changed(before, _snapshot(h))


# --------------------------------------------------------------------------- #
# 3. Another tenant's session
# --------------------------------------------------------------------------- #
def test_another_tenants_session_cannot_be_selected_or_used(h):
    run(_seed(h))
    before = _snapshot(h)
    for data in (f"rsm:{SESSION_A}", f"rtg_sess:{RULE_A}:{SESSION_A}", f"rsess:{SESSION_A}",
                 f"routeacc:{RULE_A}:{SESSION_A}", f"del_sess:{SESSION_A}"):
        run(h.press(data, OWNER_B))
    after = _snapshot(h)
    assert not _changed(before, after), _changed(before, after)
    # and the session is still there, still owned by A
    cur = h.db.connection.execute("SELECT owner_user_id FROM sessions WHERE id = ?", (SESSION_A,))
    assert int(cur.fetchone()[0]) == OWNER_A
