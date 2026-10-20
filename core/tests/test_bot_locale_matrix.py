"""Every screen renders in the language of the user who asked for it.

The catalog audit proves the *keys* are complete. It says nothing about the
render path: a panel can call `_t` without a language, resolve the process-wide
default, and serve English to a Persian user while every key exists in all ten
locales. These tests drive the real renderers and read the text and the buttons
they produce.

What is pinned here:

  * no panel is identical to its English self in any other locale;
  * a Persian panel contains Persian, a Russian one Cyrillic, a Chinese one CJK;
  * no raw catalog key and no unsubstituted `{placeholder}` reaches the screen;
  * switching language mid-session changes the very next render;
  * two concurrent renders do not share a language (the production defect this
    repository shipped once: one user's choice rendered for everyone);
  * a render outside any update context falls back to English, never Persian.
"""
from __future__ import annotations

import asyncio
import re
from typing import Callable, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

from core.domain.entities import ForwardRule
from core.infrastructure.telegram.i18n import I18n, SUPPORTED_LANGUAGES
from core.infrastructure.telegram.pro_bot_ui import ProBotUI, _BOT_RENDER_LANGUAGE

UID = 511

RTL = re.compile(r"[\u0600-\u06ff]")
CYRILLIC = re.compile(r"[\u0400-\u04ff]")
CJK = re.compile(r"[\u4e00-\u9fff]")
RAW_KEY = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
LEFTOVER_PLACEHOLDER = re.compile(r"\{[a-z_][a-z0-9_]*\}")


def build_ui() -> ProBotUI:
    return ProBotUI(
        bot=MagicMock(), i18n=I18n(), sessions=MagicMock(), rules=MagicMock(),
        filter_rules=MagicMock(), ai_configs=MagicMock(), pipeline=MagicMock(),
        pool=MagicMock(), login=MagicMock(), log_client=MagicMock(), admin_ids=[111],
    )


def build_rule(lang: str) -> ForwardRule:
    return ForwardRule(
        id="a1b2c3d4", session_id="s1", name="News feed", owner_user_id=UID,
        source_chat_id="-1001234567890", source_chat_name="Source Channel",
        target_chat_id="-1009876543210", target_chat_name="Target Channel",
    )


Panel = Tuple[str, List[str]]

PANELS: Dict[str, Callable[[ProBotUI, ForwardRule, str], Tuple[str, object]]] = {
    "welcome": lambda ui, rule, lang: ui._render_welcome_onboarding(UID, user=None),
    "rule_detail": lambda ui, rule, lang: ui._render_rule_detail(rule),
    "advanced": lambda ui, rule, lang: ui._render_advanced_settings(rule),
    "addons": lambda ui, rule, lang: ui._render_all_addons(rule),
    "draft": lambda ui, rule, lang: ui._render_draft_editor(rule, {}),
    "replace": lambda ui, rule, lang: ui._render_replace_menu(rule),
    "header_footer": lambda ui, rule, lang: ui._render_header_footer_menu(rule),
    "cleaner": lambda ui, rule, lang: ui._render_cleaner_menu(rule),
    "filters": lambda ui, rule, lang: ui._render_filters_menu(rule),
    "delay": lambda ui, rule, lang: ui._render_delay_menu(rule),
    "blacklist": lambda ui, rule, lang: ui._render_blacklist_menu(rule),
    "whitelist": lambda ui, rule, lang: ui._render_whitelist_menu(rule),
    "watermark": lambda ui, rule, lang: ui._render_watermark_menu(rule),
    "translate": lambda ui, rule, lang: ui._render_translate_menu(rule),
    "smart": lambda ui, rule, lang: ui._render_smart_menu(rule),
    "pv_menu": lambda ui, rule, lang: ui._render_pv_menu(UID, lang=lang),
}


def render(panel: str, lang: str) -> Panel:
    """Render one panel with a fresh UI whose user prefers ``lang``."""
    ui = build_ui()
    ui.remember_language(UID, lang)
    rule = build_rule(lang)
    token = _BOT_RENDER_LANGUAGE.set(lang)
    try:
        text, keyboard = PANELS[panel](ui, rule, lang)
    finally:
        _BOT_RENDER_LANGUAGE.reset(token)
    buttons = [b.text for row in getattr(keyboard, "inline_keyboard", []) or [] for b in row]
    return text or "", buttons


@pytest.fixture(scope="module")
def rendered() -> Dict[str, Dict[str, Panel]]:
    return {panel: {lang: render(panel, lang) for lang in SUPPORTED_LANGUAGES} for panel in PANELS}


# --------------------------------------------------------------------------- #
# the panel renders in the requested language
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("panel", sorted(PANELS))
@pytest.mark.parametrize("lang", [code for code in SUPPORTED_LANGUAGES if code != "en"])
def test_no_panel_falls_back_to_english(rendered, panel, lang):
    english = rendered[panel]["en"]
    other = rendered[panel][lang]
    assert other != english, (
        f"{panel} renders byte-identical text in {lang} and en — "
        f"the render path ignores the user's language"
    )


@pytest.mark.parametrize("panel", sorted(PANELS))
def test_each_panel_uses_the_script_of_its_locale(rendered, panel):
    fa_text = " ".join([rendered[panel]["fa"][0], *rendered[panel]["fa"][1]])
    ru_text = " ".join([rendered[panel]["ru"][0], *rendered[panel]["ru"][1]])
    zh_text = " ".join([rendered[panel]["zh"][0], *rendered[panel]["zh"][1]])
    assert RTL.search(fa_text), f"{panel} has no Persian text at all"
    assert CYRILLIC.search(ru_text), f"{panel} has no Russian text at all"
    assert CJK.search(zh_text), f"{panel} has no Chinese text at all"


@pytest.mark.parametrize("panel", sorted(PANELS))
@pytest.mark.parametrize("lang", SUPPORTED_LANGUAGES)
def test_no_raw_key_or_placeholder_reaches_the_screen(rendered, panel, lang):
    text, buttons = rendered[panel][lang]
    for line in text.splitlines():
        stripped = line.strip().strip("*_`• ")
        assert not RAW_KEY.match(stripped), f"{panel}/{lang} shows the raw key {stripped!r}"
    leftovers = LEFTOVER_PLACEHOLDER.findall(text + " " + " ".join(buttons))
    assert leftovers == [], f"{panel}/{lang} left {leftovers} unsubstituted"


# --------------------------------------------------------------------------- #
# per-user language, not process state
# --------------------------------------------------------------------------- #
def test_switching_language_changes_the_next_render():
    english = render("cleaner", "en")
    persian = render("cleaner", "fa")
    back_to_english = render("cleaner", "en")
    assert english != persian
    assert back_to_english == english


def test_render_outside_an_update_falls_back_to_english_never_persian():
    """Fail-closed: no context is English, not the last language anybody picked."""
    ui = build_ui()
    ui.remember_language(UID, "fa")
    ui.i18n.set_language("fa")  # simulate a stray global write
    assert _BOT_RENDER_LANGUAGE.get() is None
    assert ui._t("ui_help") == ui.i18n.t("ui_help", lang="en")
    ui.i18n.set_language("en")


async def _two_line_render(ui: ProBotUI, lang: str) -> Tuple[str, str]:
    """Render, yield to the event loop, render again.

    The yield is the point: if the language lived in shared mutable state, the
    second line would come back in whichever language the other task set.
    """
    token = _BOT_RENDER_LANGUAGE.set(lang)
    try:
        first = ui._t("ui_help")
        await asyncio.sleep(0)
        second = ui._t("ui_cancel")
        return first, second
    finally:
        _BOT_RENDER_LANGUAGE.reset(token)


def test_two_concurrent_users_do_not_share_a_language():
    ui = build_ui()
    expected = {
        lang: (ui.i18n.t("ui_help", lang=lang), ui.i18n.t("ui_cancel", lang=lang))
        for lang in ("en", "fa", "ru", "zh")
    }

    async def run():
        return await asyncio.gather(*(_two_line_render(ui, lang) for lang in expected))

    results = asyncio.run(run())
    for lang, got in zip(expected, results):
        assert got == expected[lang], f"{lang} rendered another user's language: {got}"


def test_a_slow_render_does_not_leak_into_a_fast_one():
    """Interleave a panel that awaits between its lines with a plain one."""
    ui = build_ui()
    fa_help = ui.i18n.t("ui_help", lang="fa")
    en_help = ui.i18n.t("ui_help", lang="en")

    async def slow():
        token = _BOT_RENDER_LANGUAGE.set("fa")
        try:
            await asyncio.sleep(0.01)
            return ui._t("ui_help")
        finally:
            _BOT_RENDER_LANGUAGE.reset(token)

    async def fast():
        token = _BOT_RENDER_LANGUAGE.set("en")
        try:
            return ui._t("ui_help")
        finally:
            _BOT_RENDER_LANGUAGE.reset(token)

    async def run():
        return await asyncio.gather(slow(), fast())

    got_slow, got_fast = asyncio.run(run())
    assert got_slow == fa_help
    assert got_fast == en_help


# --------------------------------------------------------------------------- #
# the command surface outside the dashboard
# --------------------------------------------------------------------------- #
def test_command_handlers_pass_the_users_language_explicitly():
    """Regression: `_sessions_cmd`, `_rules_cmd`, `_ai_cmd` and `_stats`
    rendered the list *rows* through the process-wide default while localizing
    the header above them — a half-Persian message for every Persian user."""
    import ast
    from pathlib import Path

    source_path = Path(__file__).resolve().parents[1] / "infrastructure/telegram/bot_manager.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    naked: List[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", getattr(func, "id", ""))
            if name == "t" and getattr(getattr(func, "value", None), "attr", "") == "i18n":
                if not any(kw.arg == "lang" for kw in node.keywords):
                    naked.append(node.lineno)
    assert naked == [], f"i18n.t() without lang= at lines {naked}"
