"""Every registered setting must be reachable from the Telegram bot.

The dashboard and the mobile app render the server's registry, so they cannot
drift from it. The bot's panels were hand-written, and an audit found 26 of the
72 settings had no path in the bot — a user who only has Telegram could not turn
on branding triggers, a regex filter, per-source policies or strip-words at all.

The first test here is the guard for that whole class: it walks the editor the way
a user does (groups, every page) and asserts that the set of settings it can open
equals the registry exactly. Add a setting server-side and this test tells you
immediately whether the bot can reach it.

The rest pin the write path: validated by the registry (so the bot rejects what
the API would reject), persisted through the caller's own save (so ownership is
enforced where every other bot write enforces it), and refused — never applied to
the wrong setting — when a button belongs to a screen that has moved on.
"""
from __future__ import annotations

import asyncio

import pytest

from core.domain.entities import ForwardRule
from core.domain.rule_settings import SETTING_SPECS
from core.infrastructure.telegram.registry_editor import RegistryEditor


class State:
    """The slice of UiState the editor touches."""

    def __init__(self) -> None:
        self.step = ""
        self.buffer: dict = {}


class Saver:
    """Stands in for `_save_owned_rule`: records every persisted rule."""

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def __call__(self, rule):
        self.calls += 1
        if self.fail:
            raise RuntimeError("concurrency conflict")


def t(key, **params):
    """The real catalog, so a missing key fails here rather than on a phone."""
    from core.infrastructure.telegram.i18n import I18n

    text = I18n().t(key, lang="en", **params)
    assert text and text != key, f"catalog key missing: {key}"
    return text


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def make_rule(**kwargs) -> ForwardRule:
    base = dict(id="r-1", name="t", source_chat_id="-1001", target_chat_id="-1002",
                owner_user_id=7)
    base.update(kwargs)
    return ForwardRule(**base)


def editor(saver: Saver | None = None) -> RegistryEditor:
    return RegistryEditor(translate=t, save=saver or Saver())


# --------------------------------------------------------------------------- #
# Parity: the registry and the bot's reach are the same set
# --------------------------------------------------------------------------- #


def test_every_registered_setting_is_reachable_from_the_bot():
    rule, st, ed = make_rule(), State(), editor()
    _text, rows = ed.groups(rule, st)

    reached: set[str] = set()
    groups = [
        data.split(":")[3] for row in rows for _label, data in row
        if data.startswith("rge:G:")
    ]
    assert groups, "the editor offers no categories"

    for group in groups:
        page = 0
        while True:
            _t, rows = ed.group(rule, st, group, page)
            for row in rows:
                for _label, data in row:
                    if data.startswith("rge:k:"):
                        idx = int(data.rsplit(":", 1)[1])
                        key = ed.key_at(st, idx)
                        assert key is not None, f"{group}: button {data} has no setting"
                        reached.add(key)
            # find a "next" control, else stop
            nxt = [
                data for row in rows for _l, data in row
                if data.startswith("rge:G:") and data.endswith(f":{page + 1}")
            ]
            if not nxt:
                break
            page += 1

    assert reached == set(SETTING_SPECS), (
        f"bot cannot reach {sorted(set(SETTING_SPECS) - reached)}"
    )


@pytest.mark.parametrize(
    "key",
    [
        "branding_triggers_enabled",  # whole group was absent from the bot
        "regex_block",
        "regex_require",
        "source_policies",
        "target_policies",
        "strip_words",
        "keyword_reply",
        "watermark_text",
        "whitelist_mode",
        "auto_delete_seconds",
    ],
)
def test_settings_that_had_no_bot_path_are_now_openable(key):
    rule, st, ed = make_rule(), State(), editor()
    group = SETTING_SPECS[key].group
    idx = [k for k in SETTING_SPECS if SETTING_SPECS[k].group == group].index(key)
    _t, rows = ed.group(rule, st, group, idx // ed.page_size)
    detail, _rows = ed.detail(rule, st, idx)
    label = ed.label_for(key)
    assert any(label == _label for row in rows for _label, _d in row), label
    assert label in detail
    assert ed.key_at(st, idx) == key
    assert any(len(row) for row in rows)


def test_search_finds_a_setting_by_its_description():
    rule, st, ed = make_rule(), State(), editor()
    text, rows = ed.search(rule, st, "regex")
    found = [ed.key_at(st, int(d.rsplit(":", 1)[1]))
             for row in rows for _l, d in row if d.startswith("rge:k:")]
    assert "regex_block" in found
    assert "regex" in text.lower()


def test_search_is_case_insensitive_and_reports_no_match():
    rule, st, ed = make_rule(), State(), editor()
    _t1, rows = ed.search(rule, st, "BRANDING")
    assert any(d.startswith("rge:k:") for row in rows for _l, d in row)

    text, rows = ed.search(rule, st, "zzzz-nothing")
    assert "zzzz-nothing" in text
    assert not any(d.startswith("rge:k:") for row in rows for _l, d in row)


# --------------------------------------------------------------------------- #
# Writes go through the registry, then through the owner's save
# --------------------------------------------------------------------------- #


def test_a_switch_is_written_through_the_registry_and_persisted():
    rule, st, saver = make_rule(), State(), Saver()
    ed = editor(saver)
    idx = [k for k in SETTING_SPECS if SETTING_SPECS[k].group == SETTING_SPECS["debug_mode"].group].index("debug_mode")
    ed.group(rule, st, SETTING_SPECS["debug_mode"].group, idx // ed.page_size)

    notice = run(ed.toggle(rule, st, idx))

    assert rule.debug_mode is True
    assert saver.calls == 1
    assert "Debug" in notice or notice


def test_a_rejected_value_is_not_persisted():
    rule, st, saver = make_rule(), State(), Saver()
    ed = editor(saver)
    group = SETTING_SPECS["ai_max_tokens"].group
    keys = [k for k in SETTING_SPECS if SETTING_SPECS[k].group == group]
    idx = keys.index("ai_max_tokens")
    ed.group(rule, st, group, idx // ed.page_size)

    before = rule.ai_max_tokens
    notice = run(ed.set_value(rule, st, idx, "-5"))  # below any sane bound

    assert rule.ai_max_tokens == before
    assert saver.calls == 0, "a rejected value must not reach the store"
    assert "⚠️" in notice


def test_a_non_json_dict_is_refused_with_the_reason():
    rule, st, saver = make_rule(), State(), Saver()
    ed = editor(saver)
    group = SETTING_SPECS["cleaner_config"].group
    keys = [k for k in SETTING_SPECS if SETTING_SPECS[k].group == group]
    idx = keys.index("cleaner_config")
    ed.group(rule, st, group, idx // ed.page_size)

    before = dict(rule.cleaner_config or {})
    notice = run(ed.set_value(rule, st, idx, "{not json"))

    assert rule.cleaner_config == before
    assert saver.calls == 0
    assert "JSON" in notice


def test_a_list_field_takes_a_comma_separated_line():
    rule, st, saver = make_rule(), State(), Saver()
    ed = editor(saver)
    group = SETTING_SPECS["strip_words"].group
    keys = [k for k in SETTING_SPECS if SETTING_SPECS[k].group == group]
    idx = keys.index("strip_words")
    ed.group(rule, st, group, idx // ed.page_size)

    run(ed.set_value(rule, st, idx, "spam, ads ,"))

    assert list(rule.strip_words) == ["spam", "ads"]
    assert saver.calls == 1

    run(ed.set_value(rule, st, idx, "-"))
    assert list(rule.strip_words) == []


def test_reset_returns_the_default_and_says_so():
    rule = make_rule()
    rule.debug_mode = True
    st, saver = State(), Saver()
    ed = editor(saver)
    group = SETTING_SPECS["debug_mode"].group
    keys = [k for k in SETTING_SPECS if SETTING_SPECS[k].group == group]
    idx = keys.index("debug_mode")
    ed.group(rule, st, group, idx // ed.page_size)

    notice = run(ed.reset(rule, st, idx))

    assert rule.debug_mode is False
    assert saver.calls == 1
    assert "↩️" in notice


def test_a_choice_is_set_by_position_and_the_pair_rule_still_bites():
    rule = make_rule()
    st, saver = State(), Saver()
    ed = editor(saver)
    group = SETTING_SPECS["delivery_transport"].group
    keys = [k for k in SETTING_SPECS if SETTING_SPECS[k].group == group]
    idx = keys.index("delivery_transport")
    ed.group(rule, st, group, 0)

    # BOT with no attached token is refused as a *resulting state*, even though
    # the single value looks fine on its own.
    notice = run(ed.set_choice(rule, st, idx, 1))
    assert rule.delivery_transport == "USER"
    assert saver.calls == 0
    assert "⚠️" in notice


# --------------------------------------------------------------------------- #
# Stale screens and failures
# --------------------------------------------------------------------------- #


def test_a_button_from_an_old_screen_is_refused():
    rule, st, saver = make_rule(), State(), Saver()
    ed = editor(saver)
    ed.group(rule, st, SETTING_SPECS["debug_mode"].group, 0)
    first_key = ed.key_at(st, 0)

    # The user navigated elsewhere (e.g. a search with two results).
    ed.search(rule, st, "zzzz-nothing")
    assert ed.key_at(st, 0) is None

    notice = run(ed.toggle(rule, st, 0))
    assert saver.calls == 0
    assert notice
    assert first_key is not None  # the old screen did have a setting there


def test_a_switch_only_toggles_switches():
    rule, st, saver = make_rule(), State(), Saver()
    ed = editor(saver)
    group = SETTING_SPECS["watermark_text"].group
    keys = [k for k in SETTING_SPECS if SETTING_SPECS[k].group == group]
    idx = keys.index("watermark_text")
    ed.group(rule, st, group, idx // ed.page_size)

    notice = run(ed.toggle(rule, st, idx))
    assert saver.calls == 0
    assert "watermark" in notice.lower() or "Watermark" in notice


def test_a_store_failure_is_reported_not_swallowed():
    rule, st = make_rule(), State()
    ed = editor(Saver(fail=True))
    group = SETTING_SPECS["debug_mode"].group
    keys = [k for k in SETTING_SPECS if SETTING_SPECS[k].group == group]
    idx = keys.index("debug_mode")
    ed.group(rule, st, group, idx // ed.page_size)

    notice = run(ed.toggle(rule, st, idx))
    assert "concurrency conflict" in notice


def test_the_summary_counts_what_the_task_overrides():
    rule = make_rule()
    ed = editor()
    base = ed.changed_count(rule)
    rule.debug_mode = True
    assert ed.changed_count(rule) == base + 1


# --------------------------------------------------------------------------- #
# Every kind in the registry is writable, not just the ones the panels covered
# --------------------------------------------------------------------------- #


def goto(ed: RegistryEditor, rule, st, key: str) -> int:
    """Land on the page that holds `key` and return its index in that category."""
    group = SETTING_SPECS[key].group
    keys = [k for k in SETTING_SPECS if SETTING_SPECS[k].group == group]
    idx = keys.index(key)
    ed.group(rule, st, group, idx // ed.page_size)
    return idx


@pytest.mark.parametrize(
    "key,raw,expected",
    [
        ("ai_creativity", "0.7", 0.7),           # float
        ("ai_max_tokens", "512", 512),           # int
        ("watermark_text", "sent by @example", "sent by @example"),  # str
        ("strip_words", "a, b", ["a", "b"]),     # list
        ("cleaner_config", '{"drop_empty": true}', {"drop_empty": True}),  # dict
    ],
)
def test_every_kind_the_registry_uses_can_be_set_from_the_bot(key, raw, expected):
    rule, st, saver = make_rule(), State(), Saver()
    ed = editor(saver)
    idx = goto(ed, rule, st, key)

    notice = run(ed.set_value(rule, st, idx, raw))

    assert getattr(rule, key) == expected, f"{key} = {getattr(rule, key)!r}"
    assert saver.calls == 1
    assert "⚠️" not in notice


def test_a_switch_that_is_not_a_switch_is_not_offered_for_typing():
    rule, st, saver = make_rule(), State(), Saver()
    ed = editor(saver)
    idx = goto(ed, rule, st, "ai_creativity")
    text, rows = ed.detail(rule, st, idx)
    data = [d for row in rows for _l, d in row]
    assert any(d.startswith("rge:e:") for d in data), "a number has no editor"
    assert not any(d.startswith("rge:t:") for d in data), "a number must not toggle"


def test_a_switch_offers_the_toggle_and_not_an_editor():
    rule, st, saver = make_rule(), State(), Saver()
    ed = editor(saver)
    idx = goto(ed, rule, st, "debug_mode")
    _text, rows = ed.detail(rule, st, idx)
    data = [d for row in rows for _l, d in row]
    assert any(d.startswith("rge:t:") for d in data)
    assert not any(d.startswith("rge:e:") for d in data)


def test_a_choice_setting_offers_pickable_values_not_a_typing_prompt():
    rule, st, saver = make_rule(), State(), Saver()
    ed = editor(saver)
    idx = goto(ed, rule, st, "whitelist_mode")
    _text, rows = ed.detail(rule, st, idx)
    data = [d for row in rows for _l, d in row]
    assert any(d.startswith("rge:c:") for d in data)

    _t2, choices = ed.choices(rule, st, idx)
    picks = [d for row in choices for _l, d in row if d.startswith("rge:v:")]
    assert len(picks) == len(SETTING_SPECS["whitelist_mode"].choices)
