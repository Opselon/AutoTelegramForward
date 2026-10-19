"""Regression tests for the Telegram dashboard keyboards' styling.

The dashboard (main menu, settings, admin panel) was the one surface in the
bot that never reached the button-style funnel: its rows were built as plain
``InlineKeyboardButton`` objects, so no dashboard button carried a
``keyboardButtonStyle`` background and the four glyph-less entries
(Forward / Spam / Publish / Clone) rendered as bare text in all 10 locales.

These tests render the real builders — not a fixture copy — in every
supported language and pin:

* one glyph per label, identical in all 10 locales (no per-language styling);
* the semantic colours of the action classes, and nothing else painted;
* the callback wiring, so styling never moves a button's target;
* graceful degradation when ``ATF_BUTTON_STYLES=0``.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from core.infrastructure.telegram.button_style import (
    DANGER,
    PRIMARY,
    SUCCESS,
    StyledInlineKeyboardButton,
)
from core.infrastructure.telegram.i18n import SUPPORTED_LANGUAGES, I18n
from core.infrastructure.telegram.pro_bot_ui import CB, ProBotUI
from core.infrastructure.telegram.ui_styles import (
    ANY_GLYPH,
    REPEATED_GLYPH,
    leading_glyph,
)

REPO = Path(__file__).resolve().parents[2]
BOT_UI_PATH = REPO / "core" / "infrastructure" / "telegram" / "pro_bot_ui.py"

ADMIN_UID = 5094837833
CLIENT_UID = 5989354154

#: key -> (rendered glyph, colour) for the entries the dashboard owns.
EXPECTED_DECORATION = {
    "ui_main_menu_forward": ("📤", PRIMARY),
    "ui_main_menu_filters": ("🛡️", None),
    "ui_main_menu_publish": ("➕", PRIMARY),
    "ui_main_menu_clone": ("📋", None),
    "ui_main_menu_settings": ("⚙️", None),
    "ui_main_menu_web": ("🧩", None),
    "ui_main_menu_upgrade": ("💎", SUCCESS),
    "ui_main_menu_stats": ("📊", None),
    "ui_main_menu_diag": ("🩺", None),
    "ui_main_menu_help": ("📖", None),
    "ui_main_menu_lang": ("🌐", None),
    "ui_main_menu_sessions": ("🔌", None),
}


def _ui(lang: str, *, admin: bool) -> ProBotUI:
    i18n = I18n()
    ui = object.__new__(ProBotUI)
    ui.i18n = i18n
    ui._states = {}
    ui._users = None
    ui._rules = None
    ui._db = None
    ui._error_log = None
    ui._t = lambda key, lang=None, **kw: i18n.t(key, lang=lang or "en", **kw)
    ui._resolve_lang = lambda uid=None, user_obj=None: lang
    ui._is_admin = lambda uid, _admin=admin: _admin
    return ui


def _buttons(markup):
    return [btn for row in markup.inline_keyboard for btn in row]


def _main_menu(lang: str = "en", *, admin: bool = False):
    ui = _ui(lang, admin=admin)
    return ui._main_menu(uid=ADMIN_UID if admin else CLIENT_UID)


def _by_data(markup, data: str):
    return [b for b in _buttons(markup) if getattr(b, "callback_data", None) == data]


# --------------------------------------------------------------------------- #
# decoration
# --------------------------------------------------------------------------- #
def test_no_dashboard_button_renders_without_a_glyph() -> None:
    for lang in SUPPORTED_LANGUAGES:
        for admin in (False, True):
            for btn in _buttons(_main_menu(lang, admin=admin)):
                assert btn.text.strip(), "a dashboard button rendered with no label"
                assert ANY_GLYPH.search(btn.text), (
                    f"[{lang}] dashboard button {btn.text!r} has no glyph"
                )


def test_every_dashboard_label_carries_exactly_one_leading_glyph() -> None:
    """A style prefix plus an authored catalog glyph must never double up.

    Only the *leading* glyph is decoration: "📖 Documentation ↗" ends with an
    external-link marker, which is copy, not a second decoration.
    """
    for lang in SUPPORTED_LANGUAGES:
        for admin in (False, True):
            for btn in _buttons(_main_menu(lang, admin=admin)):
                assert leading_glyph(btn.text), btn.text
                assert not REPEATED_GLYPH.match(btn.text), btn.text


def test_the_four_plain_entries_now_carry_the_dashboard_glyph() -> None:
    """Forward / Spam / Publish / Clone rendered glyph-less in all 10 locales."""
    menu = _main_menu()
    for data, (glyph, _colour) in (
        (CB["rules"], EXPECTED_DECORATION["ui_main_menu_forward"]),
        (CB["filters"], EXPECTED_DECORATION["ui_main_menu_filters"]),
        (CB["rule_add"], EXPECTED_DECORATION["ui_main_menu_publish"]),
        (CB["ai"], EXPECTED_DECORATION["ui_main_menu_clone"]),
    ):
        btn = _by_data(menu, data)[0]
        assert btn.text.startswith(glyph), btn.text


def test_no_dashboard_entry_renders_as_a_back_arrow() -> None:
    """`ui_main_menu_*` matched the rules' main|menu and resolved to nav."""
    for lang in SUPPORTED_LANGUAGES:
        for btn in _buttons(_main_menu(lang)):
            assert not btn.text.startswith("⬅️"), btn.text


# --------------------------------------------------------------------------- #
# colour: only the action to take next is painted
# --------------------------------------------------------------------------- #
def test_the_dashboard_paints_the_action_paths_and_nothing_else() -> None:
    menu = _main_menu()
    painted = {}
    for btn in _buttons(menu):
        data = getattr(btn, "callback_data", None)
        style = getattr(btn, "style", None)
        if style:
            painted[data] = style

    assert painted == {
        CB["rules"]: PRIMARY,
        CB["rule_add"]: PRIMARY,
        CB["upgrade"]: SUCCESS,
    }, painted


def test_navigation_and_information_entries_stay_unpainted() -> None:
    menu = _main_menu(admin=True)
    for data in (CB["admin"], CB["filters"], CB["ai"], CB["settings"],
                 CB["dashboard"], CB["stats"], CB["diag"], CB["help"],
                 CB["lang"], CB["sessions"]):
        for btn in _by_data(menu, data):
            assert getattr(btn, "style", None) is None, btn.text


def test_settings_and_admin_panels_are_styled_too() -> None:
    ui = _ui("en", admin=True)
    settings = _buttons(ui._client_settings_menu(uid=ADMIN_UID))
    creds = [b for b in settings if getattr(b, "callback_data", None) == CB["client_creds"]][0]
    assert getattr(creds, "style", None) == PRIMARY
    back = [b for b in settings if getattr(b, "callback_data", None) == CB["main"]][0]
    assert getattr(back, "style", None) is None

    _body, panel = asyncio.run(ui._render_admin_panel(ADMIN_UID))
    broadcast = [b for b in _buttons(panel) if getattr(b, "callback_data", None) == CB["adm_bcast"]][0]
    assert broadcast.style == PRIMARY
    for btn in _buttons(panel):
        if getattr(btn, "callback_data", None) == CB["adm_bcast"]:
            continue
        assert getattr(btn, "style", None) is None, btn.text


def test_no_destructive_colour_appears_on_the_dashboard() -> None:
    """Red marks destructive actions; the dashboard has none of them."""
    for lang in SUPPORTED_LANGUAGES:
        for admin in (False, True):
            for btn in _buttons(_main_menu(lang, admin=admin)):
                assert getattr(btn, "style", None) != DANGER, btn.text


# --------------------------------------------------------------------------- #
# locale invariance and wiring
# --------------------------------------------------------------------------- #
def test_decoration_and_colour_are_identical_in_every_locale() -> None:
    reference = [(getattr(b, "style", None), len(ANY_GLYPH.findall(b.text)))
                 for b in _buttons(_main_menu("en", admin=True))]
    for lang in SUPPORTED_LANGUAGES:
        shape = [(getattr(b, "style", None), len(ANY_GLYPH.findall(b.text)))
                 for b in _buttons(_main_menu(lang, admin=True))]
        assert shape == reference, f"{lang} styles its dashboard differently"


def test_styling_never_moves_a_button_target() -> None:
    menu = _main_menu(admin=True)
    wiring = [getattr(b, "callback_data", None) or getattr(b, "url", None)
              for b in _buttons(menu)]
    assert wiring == [
        CB["admin"],
        CB["rules"], CB["filters"],
        CB["rule_add"], CB["ai"],
        CB["settings"], CB["dashboard"],
        CB["upgrade"], CB["stats"],
        CB["diag"],
        CB["help"], CB["lang"],
        "https://t.me/Capxi",
        CB["sessions"],
    ]


def test_the_support_button_stays_a_url_button() -> None:
    """A URL button cannot carry a callback style; decoration only."""
    support = [b for b in _buttons(_main_menu()) if getattr(b, "url", None)][0]
    assert support.url == "https://t.me/Capxi"
    assert support.text.startswith("🆘")
    assert support.callback_data is None


# --------------------------------------------------------------------------- #
# degradation + no bypass
# --------------------------------------------------------------------------- #
def test_dashboard_degrades_to_plain_buttons_when_styles_are_off(monkeypatch) -> None:
    monkeypatch.setenv("ATF_BUTTON_STYLES", "0")
    menu = _main_menu()
    assert menu.inline_keyboard
    for btn in _buttons(menu):
        assert not isinstance(btn, StyledInlineKeyboardButton)
        assert getattr(btn, "style", None) in (None,)
    # the labels keep their decoration, so the panel stays readable
    assert _by_data(menu, CB["rules"])[0].text.startswith("📤")


def test_no_decorated_label_bypasses_the_style_funnel() -> None:
    """`InlineKeyboardButton(self._btn(...))` loses the class colour silently."""
    source = BOT_UI_PATH.read_text(encoding="utf-8")
    assert "InlineKeyboardButton(self._btn(" not in source


@pytest.mark.parametrize(
    "method", ["_main_menu", "_client_settings_menu", "_render_admin_panel"]
)
def test_dashboard_builders_go_through_the_funnel(method: str) -> None:
    source = inspect.getsource(getattr(ProBotUI, method))
    assert "self._kbd(" in source, f"{method} does not use the style funnel"
    assert "InlineKeyboardButton(" not in source
