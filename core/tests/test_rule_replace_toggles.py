"""Enable/disable switches for every replace-rule surface.

The per-task editor's rows and the unified `/replace` list rows each carry
their own button, a toggled managed rule (one id, many tasks) arrives the same
state everywhere it is assigned, and the engine stops (and resumes) applying a
switched-off entry.
"""

from __future__ import annotations

from typing import Dict

import pytest

from core.domain.entities import ForwardRule
from core.domain.text_replace import apply_replacements

OWNER = 5094837833
OTHER = 6278059256


def _rule(rule_id: str, replacements: Dict[str, object], rid_version: bool = False) -> ForwardRule:
    return ForwardRule(
        id=rule_id,
        session_id="sess-a",
        source_chat_id="-1004400615270",
        target_chat_id="-1003915313919",
        name=rule_id,
        owner_user_id=OWNER,
        metadata={"replacements": dict(replacements)},
    )


def _harness(tmp_path):
    from core.tests.bot_dispatch import BotHarness

    return BotHarness(str(tmp_path / "atf.db"))


def _stored(harness, rule_id: str) -> dict:
    """The persisted metadata dict of one task (the raw row stores a string)."""
    row = harness.rule_row(rule_id)
    meta = row.get("metadata")
    if isinstance(meta, str):
        import json as _json
        meta = _json.loads(meta)
    return (meta or {}).get("replacements") or {}


def _menu_rows(ui, rule):
    text, kbd = ui._render_replace_menu(rule)
    rows = kbd.inline_keyboard if hasattr(kbd, "inline_keyboard") else kbd["rows"]
    out = []
    for row in rows:
        buttons = [(b.text, b.callback_data) for b in row]
        out.append(buttons)
    return text, out


# --------------------------------------------------------------------------- #
# engine semantics
# --------------------------------------------------------------------------- #


def test_a_disabled_entry_is_not_applied_until_reenabled():
    reps = {"old": {"to": "new"}}
    assert apply_replacements("old old", reps) == "new new"
    reps["old"]["enabled"] = False
    assert apply_replacements("old old", reps) == "old old"
    del reps["old"]["enabled"]
    assert apply_replacements("old old", reps) == "new new"


# --------------------------------------------------------------------------- #
# the per-task menu
# --------------------------------------------------------------------------- #


async def test_render_menu_offers_a_switch_per_entry(tmp_path):
    harness = _harness(tmp_path)
    try:
        await harness.seed_session("sess-a", OWNER)
        rule = await harness.seed_rule("rtg-menu", OWNER, metadata={
            "replacements": {"promo": "sale", "gone": ""},
        })
        ui = harness.ui
        text, rows = _menu_rows(ui, rule)
        toggle = [b for row in rows for b in row if b[1].startswith("rtxt:rtg-menu")]
        assert len(toggle) == 2, f"every entry needs a switch button: {rows}"
        assert all("Disable" in b[0] for b in toggle)
        # No disabled marker while everything runs.
        assert "Disabled" not in text
    finally:
        harness.close()


async def test_toggling_a_manual_entry_flips_enabled(tmp_path):
    harness = _harness(tmp_path)
    try:
        await harness.seed_session("sess-a", OWNER)
        await harness.seed_rule("rtg-manual", OWNER, metadata={
            "replacements": {"promo": "sale"},
        })
        version = harness.rule_row("rtg-manual")["version"]
        cq = await harness.press(f"rtxt:rtg-manual:0:{version}", OWNER)
        entry = _stored(harness, "rtg-manual").get("promo")
        assert entry.get("enabled") is False
        assert any("Rule disabled" in t for t in cq.answers.texts)

        # The pipeline stops applying it, and applying resumes on re-enable.
        rule = (await harness.rules.list_by_owner(OWNER))[0]
        assert apply_replacements("promo", rule.replacements) == "promo"

        await harness.press(f"rtxt:rtg-manual:0:{harness.rule_row('rtg-manual')['version']}", OWNER)
        entry = _stored(harness, "rtg-manual")["promo"]
        # A re-enabled legacy entry collapses back to its plain string form.
        assert entry == "sale" or entry.get("enabled", True) is True
        fresh = (await harness.rules.list_by_owner(OWNER))[0]
        assert apply_replacements("promo", fresh.replacements) == "sale"
    finally:
        harness.close()


async def test_toggling_cascades_to_every_task_sharing_the_rule(tmp_path):
    harness = _harness(tmp_path)
    try:
        await harness.seed_session("sess-a", OWNER)
        rid = "abc123def4567890"
        shared = {"id": rid, "source": "promo", "to": "sale"}
        await harness.seed_rule("rtg-a", OWNER, metadata={"replacements": {rid: dict(shared)}})
        await harness.seed_rule("rtg-b", OWNER, metadata={"replacements": {rid: dict(shared)}})
        version = harness.rule_row("rtg-a")["version"]

        cq = await harness.press(f"rtxt:rtg-a:0:{version}", OWNER)
        assert any("Rule disabled" in t for t in cq.answers.texts)

        for rule_id in ("rtg-a", "rtg-b"):
            entry = _stored(harness, rule_id)[rid]
            assert entry.get("enabled") is False, f"{rule_id} must follow the switch"

        disabled = {"id": rid, "source": "promo", "to": "sale", "enabled": False}
        assert apply_replacements("promo", {rid: disabled}) == "promo"
    finally:
        harness.close()


async def test_toggling_is_tenant_scoped(tmp_path):
    """Another owner running the same rule id is never written by this owner."""
    harness = _harness(tmp_path)
    try:
        await harness.seed_session("sess-a", OWNER)
        await harness.seed_session("sess-b", OTHER)
        rid = "cascade1234567"
        shared = {"id": rid, "source": "promo", "to": "sale"}
        await harness.seed_rule("rtg-a", OWNER, metadata={"replacements": {rid: dict(shared)}})
        await harness.seed_rule("rtg-b", OTHER, metadata={"replacements": {rid: dict(shared)}})
        version = harness.rule_row("rtg-a")["version"]

        await harness.press(f"rtxt:rtg-a:0:{version}", OTHER)
        # Not the other's rule: refused or rejected, and nothing stored.
        entry = _stored(harness, "rtg-a")[rid]
        assert entry.get("enabled", True) is True
    finally:
        harness.close()

