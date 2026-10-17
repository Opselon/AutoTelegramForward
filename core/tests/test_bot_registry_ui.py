"""The registry editor, driven through the real bot handlers.

`test_bot_registry_editor.py` proves the editor reaches every setting and writes
through the registry. This file proves the part that only breaks in production:
that the handlers are *wired* — a tap on a real callback-data string reaches the
editor, the write lands in the owner's rule through `update_owned` (the one path
that refuses a transplanted rule id), a stranger's tap writes nothing at all, and
no screen or callback carries a secret.

The harness is the one the task-panel tests use, so these run the shipped
handlers rather than a copy of them.
"""
from __future__ import annotations

from core.domain.rule_settings import SETTING_SPECS
from core.tests.test_bot_route_panel import (
    OWNER,
    FakeCQ,
    FakeMessage,
    FakeRecord,
    a_rule,
    build_ui,
    callbacks_of,
    handler,
)

TOKEN = "7878773795:AAsecrettokenvaluethatisneverrendered123"


async def open_registry(ui, bot, rule_id="rule-1", uid=OWNER):
    cq = FakeCQ(f"rge:g:{rule_id}", uid=uid)
    await handler(bot, "_cb_reg_groups")(None, cq)
    return cq


def screen_text(cq):
    return "\n".join(text for text, _markup in cq.edits)


def screen_callbacks(cq):
    """Callback data on every keyboard shown, skipping prompt-only screens."""
    return [d for _text, markup in cq.edits if markup is not None
            for d in callbacks_of(markup)]


def refs_on_screen(cq, rule_id="rule-1"):
    return [d.rsplit(":", 1)[1] for d in screen_callbacks(cq)
            if d.startswith(f"rge:k:{rule_id}:")]


def controls(cq, prefix, rule_id="rule-1"):
    wanted = f"{prefix}{rule_id}:"
    return [d for d in screen_callbacks(cq) if d.startswith(wanted)]


async def open_key(ui, bot, rule, key, uid=OWNER):
    """Tap through to one setting exactly as a user would, and prove we got there.

    The caller then keeps tapping the controls it finds on that screen instead of
    constructing callback data by hand, so the test exercises the real wiring.
    """
    editor = ui._registry_editor(rule, uid)
    label = editor.label_for(key)
    group = SETTING_SPECS[key].group
    keys = [k for k in SETTING_SPECS if SETTING_SPECS[k].group == group]
    page = keys.index(key) // editor.page_size

    listing = FakeCQ(f"rge:G:{rule.id}:{group}:{page}", uid=uid)
    await handler(bot, "_cb_reg_group")(None, listing)
    refs = refs_on_screen(listing, rule.id)
    assert refs, f"{group} page {page} offered no settings"

    for ref in refs:
        probe = FakeCQ(f"rge:k:{rule.id}:{ref}", uid=uid)
        await handler(bot, "_cb_reg_key")(None, probe)
        if label in screen_text(probe):
            return probe, int(ref), label
    raise AssertionError(f"{key!r} ({label}) is not on {group} page {page}")


# --------------------------------------------------------------------------- #
# Reachable from the task, and the locale travels with it
# --------------------------------------------------------------------------- #


async def test_the_rule_screens_offer_the_whole_registry():
    ui, bot = build_ui(rules=[a_rule()], scoped=True)

    overview = FakeCQ("rd:rule-1")
    await handler(bot, "_cb_rule_detail")(None, overview)
    assert "rge:g:rule-1" in callbacks_of(overview.edits[-1][1])

    advanced = FakeCQ("radv:rule-1")
    await handler(bot, "_cb_rule_advanced_settings")(None, advanced)
    assert "rge:g:rule-1" in callbacks_of(advanced.edits[-1][1])


async def test_the_registry_opens_grouped_and_searchable():
    ui, bot = build_ui(rules=[a_rule()], scoped=True)
    cq = await open_registry(ui, bot)

    data = callbacks_of(cq.edits[-1][1])
    groups = {d.split(":")[3] for d in data if d.startswith("rge:G:")}
    assert groups == {SETTING_SPECS[k].group for k in SETTING_SPECS}
    assert any(d == "rge:s:rule-1" for d in data), "no search offered"


async def test_a_category_lists_its_settings_and_opens_one():
    ui, bot = build_ui(rules=[a_rule()], scoped=True)
    await open_registry(ui, bot)

    cq = FakeCQ("rge:G:rule-1:content:0")
    await handler(bot, "_cb_reg_group")(None, cq)
    assert refs_on_screen(cq)

    _probe, ref, label = await open_key(ui, bot, a_rule(), "regex_block")
    assert label and ref >= 0


async def test_an_untouched_setting_reads_as_the_default_and_a_changed_one_as_this_task():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule], scoped=True)
    await open_registry(ui, bot)

    probe, _ref, _label = await open_key(ui, bot, rule, "debug_mode")
    assert "default" in screen_text(probe)

    rule.debug_mode = True
    probe, _ref, _label = await open_key(ui, bot, rule, "debug_mode")
    assert "set on this task" in screen_text(probe)


# --------------------------------------------------------------------------- #
# A tap writes through the owner's own save path
# --------------------------------------------------------------------------- #


async def test_tapping_a_switch_persists_through_update_owned():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule], scoped=True)
    await open_registry(ui, bot)

    probe, _ref, _label = await open_key(ui, bot, rule, "debug_mode")
    toggle = controls(probe, "rge:t:")
    assert toggle, "a switch offers no toggle"

    await handler(bot, "_cb_reg_toggle")(None, FakeCQ(toggle[0]))

    assert rule.debug_mode is True
    assert ui._rules.updated and ui._rules.updated[-1] is rule


async def test_a_stranger_cannot_open_or_write_through_the_registry():
    ui, bot = build_ui(rules=[a_rule()], scoped=True)

    stranger = await open_registry(ui, bot, uid=99999)
    assert not stranger.edits, "a stranger got a screen"
    assert stranger.answers and stranger.answers[-1][1] is True, "no alert shown"

    cq = FakeCQ("rge:t:rule-1:0", uid=99999)
    await handler(bot, "_cb_reg_toggle")(None, cq)
    assert ui._rules.updated == [], "a stranger wrote to someone else's rule"
    assert cq.answers[-1][1] is True


# --------------------------------------------------------------------------- #
# Typed values: validated first, then saved
# --------------------------------------------------------------------------- #


async def test_a_typed_value_is_validated_then_saved_and_the_step_clears():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule], scoped=True)
    await open_registry(ui, bot)

    probe, _ref, label = await open_key(ui, bot, rule, "ai_max_tokens")
    edit = controls(probe, "rge:e:")
    assert edit, "a text setting offers no editor"
    await handler(bot, "_cb_reg_edit")(None, FakeCQ(edit[0]))

    st = ui._state(OWNER)
    assert st.step == "rge_edit_rule-1"
    assert st.buffer["reg_key"] == "ai_max_tokens"

    before = ui._rules.updated[:]
    bad = FakeMessage("not-a-number")
    await ui._step_registry_edit(bad, st, "not-a-number", "rule-1")
    assert ui._rules.updated == before, "an invalid value was persisted"
    assert "⚠️" in bad.replies[-1][0]
    assert st.step == ""

    good = FakeMessage("2048")
    await ui._step_registry_edit(good, st, "2048", "rule-1")
    assert rule.ai_max_tokens == 2048
    assert len(ui._rules.updated) == len(before) + 1
    assert label in good.replies[-1][0]


async def test_a_list_setting_takes_a_comma_separated_line_and_can_be_emptied():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule], scoped=True)
    await open_registry(ui, bot)

    probe, _ref, _label = await open_key(ui, bot, rule, "strip_words")
    edit = controls(probe, "rge:e:")
    await handler(bot, "_cb_reg_edit")(None, FakeCQ(edit[0]))

    st = ui._state(OWNER)
    await ui._step_registry_edit(FakeMessage("spam, ads"), st, "spam, ads", "rule-1")
    assert list(rule.strip_words) == ["spam", "ads"]

    await ui._step_registry_edit(FakeMessage("-"), st, "-", "rule-1")
    assert list(rule.strip_words) == []


async def test_search_from_the_bot_finds_settings_and_paginates():
    ui, bot = build_ui(rules=[a_rule()], scoped=True)
    await open_registry(ui, bot)

    cq = FakeCQ("rge:s:rule-1")
    await handler(bot, "_cb_reg_search")(None, cq)
    assert ui._state(OWNER).step == "rge_search_rule-1"

    msg = FakeMessage("regex")
    await ui._step_registry_search(msg, ui._state(OWNER), "regex", "rule-1")
    text, markup = msg.replies[-1]
    assert "regex" in text.lower()
    assert any(d.startswith("rge:k:rule-1:") for d in callbacks_of(markup))
    assert ui._state(OWNER).step == ""

    page = FakeCQ("rge:q:rule-1:0")
    await handler(bot, "_cb_reg_search_page")(None, page)
    assert callbacks_of(page.edits[-1][1])


async def test_a_stale_button_is_refused_instead_of_writing():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule], scoped=True)
    editor = ui._registry_editor(rule, OWNER)
    st = ui._state(OWNER)
    editor.group(rule, st, SETTING_SPECS["debug_mode"].group, 0)
    editor.search(rule, st, "zzzz-nothing")
    await ui._persist(OWNER, st)

    cq = FakeCQ("rge:t:rule-1:0")
    await handler(bot, "_cb_reg_toggle")(None, cq)

    assert ui._rules.updated == [], "a stale tap wrote to the store"
    assert cq.answers and cq.answers[-1][1] is True


# --------------------------------------------------------------------------- #
# Nothing secret, nothing oversized, no language leak
# --------------------------------------------------------------------------- #


async def test_no_registry_screen_or_callback_carries_the_bot_token():
    rule = a_rule()
    rule.bot_token_id = "b0t0k3n-0000-4000-8000-000000000001"
    rule.delivery_transport = "BOT"
    ui, bot = build_ui(rules=[rule], tokens=[FakeRecord(token=TOKEN)], scoped=True)
    await open_registry(ui, bot)

    for page in range(3):
        cq = FakeCQ(f"rge:G:rule-1:route:{page}")
        await handler(bot, "_cb_reg_group")(None, cq)
        assert TOKEN not in screen_text(cq)
        for _text, markup in cq.edits:
            assert all(TOKEN not in d for d in screen_callbacks(cq))


async def test_every_registry_callback_fits_the_64_byte_limit():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule], scoped=True)
    await open_registry(ui, bot)

    screens = [("_cb_reg_groups", "rge:g:rule-1"),
               ("_cb_reg_group", "rge:G:rule-1:content:0"),
               ("_cb_reg_key", "rge:k:rule-1:0"),
               ("_cb_reg_search", "rge:s:rule-1")]
    for name, data in screens:
        cq = FakeCQ(data)
        await handler(bot, name)(None, cq)
        for cb in screen_callbacks(cq):
            assert len(cb.encode("utf-8")) < 64, f"{cb} is too long"


async def test_the_registry_follows_the_tenant_language_not_the_process():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule], scoped=True)

    ui._state(OWNER).lang = "fa"
    cq = await open_registry(ui, bot)
    assert "همه تنظیمات" in screen_text(cq), screen_text(cq)
    assert "رسانه" in screen_text(cq), screen_text(cq)

    ui._state(OWNER).lang = "en"
    cq = await open_registry(ui, bot)
    assert "All settings" in screen_text(cq), screen_text(cq)
    assert "Media" in screen_text(cq), screen_text(cq)
