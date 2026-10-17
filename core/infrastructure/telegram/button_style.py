"""Bot-button styles for inline keyboards (keyboardButtonStyle, Layer 225).

Pyrogram 2.0.106 ships TL layer 158 and has no notion of a button style: its
``InlineKeyboardButton`` cannot express bg_primary/bg_danger/bg_success, and its
``keyboardButtonCallback`` constructor id (0x35bbdb6b) predates the style field.
Telegram renumbers a constructor when a field is added, so the styled button is
a *different* constructor:

    keyboardButtonCallback#e62bc960
        flags:#
        requires_password:flags.0?true
        style:flags.10?KeyboardButtonStyle
        text:string
        data:bytes

    keyboardButtonStyle#4fdd3430
        flags:#
        bg_primary:flags.0?true
        bg_danger:flags.1?true
        bg_success:flags.2?true
        icon:flags.3?long

The bytes below are written by hand because neither type exists in the
installed pyrogram; they must stay in lock-step with
https://core.telegram.org/constructor/keyboardButtonCallback and
https://core.telegram.org/constructor/keyboardButtonStyle

Field order matters in TL: the conditional ``style`` is serialised *before*
``text``/``data``, in the order the schema declares it. ``?true`` flags write no
bytes, only the flag bit.

If a client or server ever rejects the styled constructor, the UI degrades on
the first error instead of breaking: see ``disable_styles()`` and the retry in
``pro_bot_ui._safe_edit``.
"""
from __future__ import annotations

import logging
import os
from io import BytesIO
from typing import Any, Optional

from pyrogram.raw.core import Bytes, Int, Long, String, TLObject
from pyrogram.types import InlineKeyboardButton

logger = logging.getLogger("atf.button_style")

STYLE_CONSTRUCTOR = 0x4FDD3430
CALLBACK_CONSTRUCTOR = 0xE62BC960
STYLE_FLAG = 1 << 10
REQUIRES_PASSWORD_FLAG = 1 << 0
ICON_FLAG = 1 << 3

PRIMARY = "primary"   # dark blue — main actions
DANGER = "danger"     # red — destructive actions
SUCCESS = "success"   # green — positive actions

_COLOR_FLAGS = {PRIMARY: 1 << 0, DANGER: 1 << 1, SUCCESS: 1 << 2}

_DISABLED = False


def styles_enabled() -> bool:
    """Whether this process still sends styled buttons.

    ``ATF_BUTTON_STYLES=0`` turns the feature off without a redeploy; the
    runtime flag flips to off by itself the first time a styled keyboard is
    rejected, so an old client can never leave a panel unrenderable.
    """
    if _DISABLED:
        return False
    return os.environ.get("ATF_BUTTON_STYLES", "1").strip().lower() not in ("0", "false", "no", "off")


def disable_styles(reason: str = "") -> None:
    global _DISABLED
    if not _DISABLED:
        _DISABLED = True
        logger.warning("[UI:STYLE] styled buttons disabled for this process (%s)", reason or "rejected")


class KeyboardButtonStyle(TLObject):
    """``keyboardButtonStyle`` — a background colour and/or a custom-emoji icon."""

    __slots__ = ["color", "icon"]
    ID = STYLE_CONSTRUCTOR
    QUALNAME = "types.KeyboardButtonStyle"

    def __init__(self, color: Optional[str] = None, icon: Optional[int] = None) -> None:
        self.color = color
        self.icon = icon

    def write(self, *args: Any) -> bytes:
        flags = _COLOR_FLAGS.get(self.color or "", 0)
        if self.icon:
            flags |= ICON_FLAG
        out = BytesIO()
        out.write(Int(self.ID, False))
        out.write(Int(flags))
        if self.icon:
            out.write(Long(self.icon))
        return out.getvalue()


class StyledKeyboardButtonCallback(TLObject):
    """``keyboardButtonCallback#e62bc960`` — a callback button carrying a style."""

    __slots__ = ["text", "data", "style", "requires_password"]
    ID = CALLBACK_CONSTRUCTOR
    QUALNAME = "types.KeyboardButtonCallback"

    def __init__(
        self,
        *,
        text: str,
        data: bytes,
        style: Optional[KeyboardButtonStyle] = None,
        requires_password: Optional[bool] = None,
    ) -> None:
        self.text = text
        self.data = data
        self.style = style
        self.requires_password = requires_password

    def write(self, *args: Any) -> bytes:
        flags = 0
        if self.requires_password:
            flags |= REQUIRES_PASSWORD_FLAG
        if self.style is not None:
            flags |= STYLE_FLAG
        out = BytesIO()
        out.write(Int(self.ID, False))
        out.write(Int(flags))
        if self.style is not None:
            out.write(self.style.write())
        out.write(String(self.text))
        out.write(Bytes(self.data))
        return out.getvalue()


class StyledInlineKeyboardButton(InlineKeyboardButton):
    """An ``InlineKeyboardButton`` that also emits a ``keyboardButtonStyle``.

    Subclasses the pyrogram type so every existing code path (markup builders,
    ``.write()``, ``isinstance`` checks) keeps working; only the serialised
    constructor differs.
    """

    def __init__(self, text: str, callback_data: Any = None, *, style: Optional[str] = None,
                 icon: Optional[int] = None) -> None:
        super().__init__(text=text, callback_data=callback_data)
        self.style = style
        self.icon = icon

    def plain(self) -> InlineKeyboardButton:
        """The same button without a style — used by the degradation retry."""
        return InlineKeyboardButton(self.text, callback_data=self.callback_data)

    async def write(self, client):  # type: ignore[override]
        if not styles_enabled() or not self.style:
            return await super().write(client)
        data = bytes(self.callback_data, "utf-8") if isinstance(self.callback_data, str) else self.callback_data
        return StyledKeyboardButtonCallback(
            text=self.text,
            data=data,
            style=KeyboardButtonStyle(color=self.style, icon=self.icon),
        )


def button(text: str, callback_data: Any = None, style: Optional[str] = None) -> InlineKeyboardButton:
    """Build a styled button when styles are on, a plain one otherwise."""
    if style and styles_enabled():
        return StyledInlineKeyboardButton(text, callback_data, style=style)
    return InlineKeyboardButton(text, callback_data=callback_data)


def is_style_rejection(exc: BaseException) -> bool:
    """True when an error is plausibly caused by the styled constructor."""
    text = str(exc).upper()
    return any(marker in text for marker in ("CONSTRUCTOR", "STYLE", "BUTTON", "UNKNOWN_METHOD", "RPC_CALL_FAIL"))


def plain_markup(markup):
    """Rebuild a keyboard with the style stripped, preserving rows and data."""
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return markup
    from pyrogram.types import InlineKeyboardMarkup

    rows = []
    for row in markup.inline_keyboard:
        rows.append([b.plain() if isinstance(b, StyledInlineKeyboardButton) else b for b in row])
    return InlineKeyboardMarkup(rows)
