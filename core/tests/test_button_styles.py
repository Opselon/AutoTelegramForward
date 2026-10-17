"""Button styles (keyboardButtonStyle) and the task panel's button wiring.

Two guarantees:
  * the hand-written TL bytes for the styled constructors match the published
    schema (a wrong field order or flag bit is silently accepted by the server
    and renders as an unstyled — or broken — keyboard);
  * every button the task panel renders has a registered handler, and the panel
    carries exactly one way out (it used to render both "Cancel Edit" and a
    second "Back to Rule" row).
"""
import re
from pathlib import Path

import pytest

from core.infrastructure.telegram import button_style as bs
from core.infrastructure.telegram.button_style import (
    DANGER,
    PRIMARY,
    SUCCESS,
    KeyboardButtonStyle,
    StyledInlineKeyboardButton,
    StyledKeyboardButtonCallback,
)

PANEL_SRC = Path(__file__).resolve().parents[1] / "infrastructure" / "telegram" / "pro_bot_ui.py"


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "little")


# --------------------------------------------------------------------------- #
# TL serialisation
# --------------------------------------------------------------------------- #


def test_styled_callback_button_matches_the_published_constructor():
    raw = StyledKeyboardButtonCallback(
        text="Save", data=b"ed_save:1", style=KeyboardButtonStyle(color=SUCCESS)
    ).write()

    assert raw[:4] == _u32(0xE62BC960), "keyboardButtonCallback#e62bc960"
    assert raw[4:8] == _u32(1 << 10), "style lives at flag bit 10"
    # The conditional field is serialised before text/data, in schema order.
    assert raw[8:12] == _u32(0x4FDD3430), "keyboardButtonStyle#4fdd3430"
    assert raw[12:16] == _u32(1 << 2), "bg_success"

    rest = raw[16:]
    assert rest[0] == 4 and rest[1:5] == b"Save"
    assert rest[8] == 9 and rest[9:18] == b"ed_save:1"


@pytest.mark.parametrize("color,bit", [(PRIMARY, 0), (DANGER, 1), (SUCCESS, 2)])
def test_each_colour_sets_exactly_its_own_flag(color, bit):
    raw = KeyboardButtonStyle(color=color).write()
    assert raw[:4] == _u32(0x4FDD3430)
    assert raw[4:8] == _u32(1 << bit)


def test_no_colour_and_an_icon_only_are_both_valid():
    assert KeyboardButtonStyle().write()[4:8] == _u32(0)

    icon = 5_000_000_000_000_000_000
    raw = KeyboardButtonStyle(icon=icon).write()
    assert raw[4:8] == _u32(1 << 3), "icon flag"
    assert raw[8:16] == icon.to_bytes(8, "little"), "icon id is a long"


def test_requires_password_keeps_its_own_flag():
    raw = StyledKeyboardButtonCallback(
        text="t", data=b"d", style=KeyboardButtonStyle(color=PRIMARY), requires_password=True
    ).write()
    assert raw[4:8] == _u32((1 << 0) | (1 << 10))


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_styles_off_falls_back_to_the_layer_158_constructor(monkeypatch):
    monkeypatch.setenv("ATF_BUTTON_STYLES", "0")
    btn = StyledInlineKeyboardButton("Save", "ed_save:1", style=SUCCESS)

    raw = await btn.write(None)

    assert raw.ID == 0x35BBDB6B, "plain keyboardButtonCallback"
    assert raw.text == "Save"
    assert raw.data == b"ed_save:1"


@pytest.mark.asyncio
async def test_styles_on_emits_the_styled_constructor(monkeypatch):
    monkeypatch.setenv("ATF_BUTTON_STYLES", "1")
    btn = StyledInlineKeyboardButton("Save", "ed_save:1", style=SUCCESS)

    raw = await btn.write(None)

    assert isinstance(raw, StyledKeyboardButtonCallback)
    assert raw.style.color == SUCCESS


def test_disable_styles_is_sticky_and_logged():
    bs.disable_styles("test")
    try:
        assert bs.styles_enabled() is False
    finally:
        bs._DISABLED = False


def test_plain_markup_strips_styles_but_keeps_data():
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    markup = InlineKeyboardMarkup([
        [StyledInlineKeyboardButton("Save", "ed_save:1", style=SUCCESS)],
        [InlineKeyboardButton("Plain", callback_data="x:1")],
    ])

    plain = bs.plain_markup(markup)

    assert not any(isinstance(b, StyledInlineKeyboardButton) for row in plain.inline_keyboard for b in row)
    assert plain.inline_keyboard[0][0].callback_data == "ed_save:1"
    assert plain.inline_keyboard[1][0].callback_data == "x:1"


def test_a_constructor_rejection_is_recognised():
    assert bs.is_style_rejection(Exception("400: CONSTRUCTOR_NOT_FOUND"))
    assert bs.is_style_rejection(Exception("400: STYLE_INVALID"))
    assert not bs.is_style_rejection(Exception("400: CHAT_WRITE_FORBIDDEN"))


# --------------------------------------------------------------------------- #
# Panel wiring
# --------------------------------------------------------------------------- #


def test_kbd_accepts_styled_triples_and_plain_pairs(monkeypatch):
    monkeypatch.setenv("ATF_BUTTON_STYLES", "1")
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI

    ui = ProBotUI.__new__(ProBotUI)
    markup = ProBotUI._kbd(ui, [[("Save", "ed_save:1", SUCCESS), ("Cancel", "ed_can:1")]])

    assert isinstance(markup.inline_keyboard[0][0], StyledInlineKeyboardButton)
    assert markup.inline_keyboard[0][0].style == SUCCESS
    assert type(markup.inline_keyboard[0][1]).__name__ == "InlineKeyboardButton"


def test_every_task_panel_button_has_a_handler():
    src = PANEL_SRC.read_text(encoding="utf-8")
    panel = src[src.index("def _render_draft_editor"):src.index("def _render_replace_menu")]
    prefixes = set(re.findall(r'f"([a-z_]+):\{rule\.id\}"', panel))
    registered = set(re.findall(r'filters\.regex\(r"\^([a-z_]+):', src))

    assert len(prefixes) >= 15, "callback parser drifted — it found almost nothing"
    missing = sorted(p for p in prefixes if p not in registered)
    assert not missing, f"panel buttons with no handler: {missing}"


def test_the_task_panel_has_one_way_out():
    src = PANEL_SRC.read_text(encoding="utf-8")
    panel = src[src.index("def _render_draft_editor"):src.index("def _render_replace_menu")]

    assert 'f"ed_save:{rule.id}"' in panel, "Save must exist"
    assert 'f"ed_can:{rule.id}"' in panel, "Cancel must exist"
    assert 'f"rd:{rule.id}"' not in panel, (
        "the panel must not render a second back button next to Cancel Edit"
    )
