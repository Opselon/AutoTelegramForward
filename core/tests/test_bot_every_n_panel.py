"""The "forward every N messages" panel, driven through the real bot handlers.

The engine's arithmetic is covered in `test_forward_every_n.py`. This file owns
the wiring the engine cannot see: a tap on the add-on row opens the interval
screen instead of flipping a boolean, presets and the free-text step both write
the *number* the engine reads, a crafted or out-of-range callback changes
nothing, and a stranger's tap writes nothing at all.
"""

from __future__ import annotations

from core.tests.test_bot_route_panel import (
    OWNER,
    FakeCQ,
    FakeMessage,
    a_rule,
    build_ui,
    callbacks_of,
    handler,
    texts_of,
)

ROW = "radon:rule-1:every_n"


def screen_text(cq) -> str:
    return "\n".join(text for text, _markup in cq.edits)


def screen_callbacks(cq) -> list[str]:
    return [d for _text, markup in cq.edits if markup is not None
            for d in callbacks_of(markup)]


async def open_panel(ui, bot, uid=OWNER):
    cq = FakeCQ(ROW, uid=uid)
    await handler(bot, "_cb_rule_addon_toggle")(None, cq)
    return cq


async def tap(bot, data, uid=OWNER):
    cq = FakeCQ(data, uid=uid)
    name = "_cb_rule_every_n_set" if data.startswith("ren:") else "_cb_rule_every_n_custom"
    await handler(bot, name)(None, cq)
    return cq


async def send_text(ui, text, uid=OWNER):
    """The text router is not a callback: drive the method the message handler calls."""
    message = FakeMessage(text, uid=uid)
    await ui._route_text(message)
    return message


def test_the_row_opens_the_interval_screen_instead_of_toggling():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule])

    cq = FakeCQ(ROW)
    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        handler(bot, "_cb_rule_addon_toggle")(None, cq)
    )
    # The old behaviour flipped paid_addons["every_n"] and redrew the card; the
    # number the engine reads must not be reachable only through a boolean.
    assert rule.forward_every_n == 1
    callbacks = screen_callbacks(cq)
    assert any(d.startswith("ren:rule-1:") for d in callbacks)
    assert "renc:rule-1" in callbacks
    assert ui._rules.updated == []


def test_a_preset_writes_the_number_the_engine_reads():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule])

    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        tap(bot, "ren:rule-1:5")
    )
    assert rule.forward_every_n == 5
    assert rule.paid_addons.get("every_n") is True
    assert ui._rules.updated and ui._rules.updated[-1] is rule


def test_going_back_to_one_turns_the_flags_off_together():
    rule = a_rule()
    rule.forward_every_n = 5
    rule.paid_addons = {"every_n": True}
    ui, bot = build_ui(rules=[rule])

    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        tap(bot, "ren:rule-1:1")
    )
    assert rule.forward_every_n == 1
    assert "forward_every_n" not in (rule.metadata or {})
    assert rule.paid_addons.get("every_n") is False


def test_a_crafted_callback_cannot_store_an_out_of_range_interval():
    import asyncio

    for crafted in ("ren:rule-1:0", "ren:rule-1:1001", "ren:rule-1:99999999"):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        cq = FakeCQ(crafted)
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            handler(bot, "_cb_rule_every_n_set")(None, cq)
        )
        assert rule.forward_every_n == 1, crafted
        assert ui._rules.updated == [], crafted
        assert cq.answers and cq.answers[-1][1] is True  # an alert, not a silent no-op


def test_a_stranger_cannot_change_someone_elses_task():
    import asyncio

    rule = a_rule()
    ui, bot = build_ui(rules=[rule])
    cq = FakeCQ("ren:rule-1:7", uid=OWNER + 1)
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        handler(bot, "_cb_rule_every_n_set")(None, cq)
    )
    assert rule.forward_every_n == 1
    assert ui._rules.updated == []


def test_the_step_accepts_whole_numbers_at_the_bounds():
    import asyncio

    for text, expected in (("1", 1), ("2", 2), ("999", 999), ("1000", 1000), ("  7  ", 7)):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        st = ui._state(OWNER)
        st.step = "set_every_n_rule-1"
        message = FakeMessage(text)
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            ui._route_text(message)
        )
        assert rule.forward_every_n == expected, text
        assert st.step == ""
        assert message.replies and "forward_every_n" not in message.replies[-1][0]


def test_the_step_refuses_everything_that_is_not_an_interval():
    import asyncio

    # NB: "١٠" would pass — int() accepts Unicode digits — which is the
    # behaviour a Persian or Arabic keyboard produces, and it is correct.
    for text in ("0", "-2", "1001", "2.5", "abc", "", "1e3"):
        rule = a_rule()
        ui, bot = build_ui(rules=[rule])
        st = ui._state(OWNER)
        st.step = "set_every_n_rule-1"
        message = FakeMessage(text)
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            ui._route_text(message)
        )
        assert rule.forward_every_n == 1, text
        assert ui._rules.updated == [], text
        # The prompt stays open the way the delay step does: a typo asks again.
        assert st.step == "set_every_n_rule-1", text


def test_the_step_namespace_survives_a_restart():
    """The FSM validates its own step namespace; an unlisted prefix is dropped
    silently, which would show the prompt and then ignore the reply."""
    from core.infrastructure.telegram.ui_state_manager import _safe_step

    assert _safe_step("set_every_n_rule-1") == "set_every_n_rule-1"
    assert _safe_step(f"set_every_n_{'a' * 80}") != ""  # ids are opaque but bounded
    assert _safe_step("set_every_n_") == ""            # no scope at all


def test_the_custom_button_opens_a_prompt_that_can_be_cancelled():
    import asyncio

    rule = a_rule()
    rule.forward_every_n = 5
    ui, bot = build_ui(rules=[rule])
    cq = FakeCQ("renc:rule-1")
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        handler(bot, "_cb_rule_every_n_custom")(None, cq)
    )
    st = ui._state(OWNER)
    assert st.step == "set_every_n_rule-1"
    # Cancel returns to the interval screen, not to a value-less card.
    assert "ren:rule-1:5" in screen_callbacks(cq)


def test_the_task_card_shows_the_configured_interval():
    import asyncio

    rule = a_rule()
    rule.forward_every_n = 5
    ui, bot = build_ui(rules=[rule])
    cq = FakeCQ("rd:rule-1")
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        handler(bot, "_cb_rule_detail")(None, cq)
    )
    labels = [t for _text, markup in cq.edits if markup is not None for t in texts_of(markup)]
    row = [label for label in labels if "5" in label]
    assert row, labels
    assert ROW in screen_callbacks(cq)
