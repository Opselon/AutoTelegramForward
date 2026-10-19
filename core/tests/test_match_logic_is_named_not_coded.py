"""The condition logic is named, not coded.

A task stores its VIP/whitelist matching logic as our own value word —
`detection_criteria["match_mode"]`, `ANY` or `ALL` — and the smart panel printed
it raw in three places: beside a translated description on the logic line, alone
on the toggle button, and on the simulator's "combined logic" line. A Persian
user who set the logic from the panel read `منطق: ANY` and
`منطق ترکیب شروط VIP: `ANY` (حداقل یکی از شروط)` — a translated sentence wrapped
around an English token that the sentence itself already translated.

The two words are not new vocabulary: the toggle's own alert has named them
since it shipped (`ui_logic_all` / `ui_logic_any`). This file pins that they are
the *only* wording, that the templates no longer carry a second placeholder for
the token, and that every render site goes through `ProBotUI._logic_word()`.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.domain.entities import ForwardRule
from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram import pro_bot_ui as bot_ui

CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "infrastructure"
    / "telegram"
    / "locales"
    / "translations.json"
)
SOURCE = (Path(__file__).resolve().parents[1] / "infrastructure" / "telegram" / "pro_bot_ui.py")

LOCALES = ("en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it")
PERSIAN = re.compile(r"[\u0600-\u06ff]")
CYRILLIC = re.compile(r"[\u0400-\u04ff]")
HAN = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
PLACEHOLDERS = re.compile(r"\{([^{}]+)\}")
#: `ANY` / `ALL` as a standalone token, which is how the panel printed them.
TOKEN = re.compile(r"(?<![A-Za-z])(?:ANY|ALL)(?![A-Za-z])")


def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def source() -> str:
    return SOURCE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The templates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("locale", LOCALES)
def test_the_logic_line_states_the_logic_without_the_token(locale: str):
    template = catalog()[locale]["ui_smart_match_logic"]
    assert "{match_mode}" not in template, template
    assert set(PLACEHOLDERS.findall(template)) == {"mode_desc"}, template


@pytest.mark.parametrize("locale", LOCALES)
def test_the_logic_button_holds_a_word(locale: str):
    template = catalog()[locale]["ui_smart_btn_logic"]
    assert "{match_mode}" not in template, template
    assert set(PLACEHOLDERS.findall(template)) == {"mode"}, template


@pytest.mark.parametrize("locale,pattern", [
    ("fa", PERSIAN),
    ("ru", CYRILLIC),
    ("zh", HAN),
])
def test_the_words_the_panel_prints_are_written_in_that_locale_s_script(locale: str, pattern):
    cat = catalog()
    for key in ("ui_logic_all", "ui_logic_any"):
        assert pattern.search(cat[locale][key]), f"{key} is not {locale}: {cat[locale][key]!r}"


def test_no_template_anywhere_prints_a_bare_logic_token():
    """The class, not the three instances: no catalog value carries `{match_mode}`."""
    cat = catalog()
    carriers = sorted(
        key
        for key, template in cat["en"].items()
        if isinstance(template, str) and "{match_mode}" in template
    )
    assert carriers == [], f"templates that print the stored token: {carriers}"


# --------------------------------------------------------------------------- #
# One vocabulary for the two words
# --------------------------------------------------------------------------- #


def test_the_logic_words_are_the_ones_the_toggle_alert_names():
    assert bot_ui.LOGIC_WORDS == {"ALL": "ui_logic_all", "ANY": "ui_logic_any"}
    cat = catalog()
    for locale in LOCALES:
        for key in bot_ui.LOGIC_WORDS.values():
            assert str(cat.get(locale, {}).get(key, "")).strip(), f"{key} missing in {locale}"


def test_the_toggle_still_uses_those_words():
    """A second wording for the same two states is how a panel drifts."""
    text = source()
    handler = text.split("async def _cb_smart_toggle_match_mode", 1)[1].split("@b.on_callback_query", 1)[0]
    assert 'self._t("ui_logic_all")' in handler and 'self._t("ui_logic_any")' in handler
    # The alert is the only place that *renders* these two keys; everywhere else
    # reaches them through `LOGIC_WORDS`.
    for key in bot_ui.LOGIC_WORDS.values():
        rendered = text.replace(handler, "").count(f'self._t("{key}"')
        assert rendered == 0, f"{key} is rendered outside the toggle alert ({rendered}x)"


# --------------------------------------------------------------------------- #
# The render sites
# --------------------------------------------------------------------------- #


def render_calls(key: str) -> list:
    """The source of every `_t("<key>", …)` call, whatever it spans."""
    text = source()
    tree = ast.parse(text)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == key:
            out.append(ast.get_source_segment(text, node).replace("\n", " "))
    return out


@pytest.mark.parametrize("key", ["ui_smart_btn_logic", "ui_sim_analysis"])
def test_every_render_site_goes_through_the_helper(key: str):
    calls = render_calls(key)
    assert calls, f"no call site for {key}"
    assert all("self._logic_word(" in call for call in calls), (
        f"{key} is rendered with a raw value: {calls}"
    )


def test_no_call_site_passes_the_stored_token_into_a_template():
    text = source()
    offenders = [
        line.strip()
        for line in text.splitlines()
        if "match_mode=" in line and "_t(" in line
    ]
    assert offenders == [], f"the stored token still reaches a template: {offenders}"


class FakeStats:
    started_at = 1_000_000
    processed = 120
    errors = 3


def build_ui(lang: str = "fa"):
    ui = bot_ui.ProBotUI(
        bot=MagicMock(), i18n=I18n(), sessions=MagicMock(), rules=MagicMock(),
        filter_rules=MagicMock(), ai_configs=MagicMock(),
        pipeline=SimpleNamespace(stats=FakeStats()),
        pool=SimpleNamespace(all_clients=lambda: []), login=MagicMock(),
        log_client=MagicMock(), admin_ids=[111], error_log=None, metrics=None,
        dispatcher=None,
    )
    # The task's own language resolution has its own tests; this file is about
    # what the panel does with the word once it has one.
    ui._lang_for = lambda *sources, **kw: lang
    return ui


def make_rule(mode: str) -> ForwardRule:
    rule = ForwardRule(id="r-1", name="t", source_chat_id="-1001",
                       target_chat_id="-1002", owner_user_id=7)
    rule.detection_criteria = {"match_mode": mode}
    return rule


def logic_button(markup, rule_id: str = "r-1") -> str:
    buttons = [b for row in markup.inline_keyboard for b in row
               if str(b.callback_data).startswith("rsmmod:")]
    assert buttons, "the logic toggle disappeared"
    # The payload is unchanged: an in-flight keyboard keeps working.
    assert [b.callback_data for b in buttons] == [f"rsmmod:{rule_id}"]
    return buttons[0].text


@pytest.mark.parametrize("mode,desc_key,word_key", [
    ("ANY", "ui_smart_match_any", "ui_logic_any"),
    ("ALL", "ui_smart_match_all", "ui_logic_all"),
])
def test_the_smart_menu_prints_the_word_and_not_the_code(mode: str, desc_key: str, word_key: str):
    ui = build_ui("fa")
    text, markup = ui._render_smart_menu(make_rule(mode))
    cat = catalog()["fa"]

    logic_lines = [line for line in text.splitlines() if cat[desc_key] in line]
    assert logic_lines, f"{mode} has no logic line:\n{text}"
    assert not TOKEN.search(text), f"the stored token is still printed:\n{text}"

    label = logic_button(markup)
    assert cat[word_key] in label, label
    assert not TOKEN.search(label), label


def test_an_unknown_stored_logic_is_shown_as_stored():
    ui = build_ui("fa")
    assert ui._logic_word("SMART") == "SMART"
    assert ui._logic_word("") == ""
    _text, markup = ui._render_smart_menu(make_rule("SMART"))
    assert "SMART" in logic_button(markup)


def test_the_helper_speaks_the_language_it_is_asked_for():
    ui = build_ui("fa")
    assert ui._logic_word("ANY", lang="ru") == catalog()["ru"]["ui_logic_any"]
    assert PERSIAN.search(ui._logic_word("ALL", lang="fa"))


def test_the_simulator_line_is_rendered_with_a_word():
    """The dry-run screen's "combined logic" line, on a real rule."""
    ui = build_ui("fa")
    word = catalog()["fa"]["ui_logic_any"]
    rendered = ui._t("ui_sim_analysis", lang="fa", mode=ui._logic_word("ANY", lang="fa"),
                     kws="-", rx="-", reason="-")
    assert word in rendered
    assert not TOKEN.search(rendered), rendered


def test_the_render_helper_survives_a_missing_criteria_block():
    ui = build_ui("fa")
    rule = ForwardRule(id="r-2", name="t", source_chat_id="-1", target_chat_id="-2",
                       owner_user_id=7)
    rule.detection_criteria = {}
    text, markup = ui._render_smart_menu(rule)
    # No `match_mode` stored means the panel falls back to its documented
    # default, and says so in words like every other state.
    assert catalog()["fa"]["ui_smart_match_any"] in text
    assert catalog()["fa"]["ui_logic_any"] in logic_button(markup, rule.id)


def test_an_earlier_test_left_no_render_language_behind():
    """Every assertion here reads a rendered line, so a leaked `fa` would hide."""
    assert bot_ui._BOT_RENDER_LANGUAGE.get(None) is None
