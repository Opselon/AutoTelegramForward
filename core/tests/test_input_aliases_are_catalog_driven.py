"""Regression tests: input aliases come from the catalog, per locale.

Defect: the bot's multi-step flows matched a *hardcoded* tuple
``("/cancel", "انصراف", "لغو", "بازگشت")`` — sometimes spelled as ``\\u``
escapes, which no audit could see. Two consequences:

1. a user typing the cancel/clear/default word in their own language was not
   understood (only English and Persian were), and
2. the behaviour drifted: 21 call sites had grown three different word lists,
   and the escaped literals hid them from the cross-language scan.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.infrastructure.telegram.i18n import I18n, SUPPORTED_LANGUAGES
from core.infrastructure.telegram.pro_bot_ui import (
    ProBotUI,
    UiState,
    _input_aliases,
    cancel_aliases,
)

REPO = Path(__file__).resolve().parents[2]
UI_SOURCE = REPO / "core" / "infrastructure" / "telegram" / "pro_bot_ui.py"
CATALOG_PATH = (
    REPO / "core" / "infrastructure" / "telegram" / "locales" / "translations.json"
)

ALIAS_KEYS = (
    "ui_input_cancel_aliases",
    "ui_input_clear_aliases",
    "ui_input_default_aliases",
)


def _catalog() -> dict[str, dict[str, str]]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# catalog contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", ALIAS_KEYS)
def test_alias_key_is_defined_in_every_locale(key: str) -> None:
    catalog = _catalog()
    reference = catalog["en"].get(key)
    assert reference, f"{key} is missing from the English reference catalog"
    for lang in SUPPORTED_LANGUAGES:
        value = catalog[lang].get(key, "")
        assert value.strip(), f"{key} is missing for {lang}"
        assert ",," not in value and not value.startswith(","), f"{key}/{lang} malformed"


def test_english_cancel_alias_is_not_persian() -> None:
    """The reference locale must not carry another language's word."""
    assert _catalog()["en"]["ui_input_cancel_aliases"] == "cancel"


# --------------------------------------------------------------------------- #
# resolved alias sets
# --------------------------------------------------------------------------- #
def test_cancel_aliases_include_the_ascii_command() -> None:
    assert "/cancel" in cancel_aliases()


@pytest.mark.parametrize(
    "word",
    ["cancel", "لغو", "انصراف", "отмена", "назад", "取消", "返回", "annuler", "annulla", "batal"],
)
def test_cancel_aliases_cover_the_supported_locales(word: str) -> None:
    assert word in cancel_aliases(), f"{word!r} is not accepted as a cancel word"


def test_clear_and_default_aliases_are_locale_complete() -> None:
    clear = _input_aliases("ui_input_clear_aliases")
    default = _input_aliases("ui_input_default_aliases")
    for word in ("clear", "حذف", "очистить", "清除", "limpar"):
        assert word in clear, f"{word!r} is not accepted as a clear word"
    for word in ("default", "-", "پیش‌فرض", "по умолчанию", "默认", "predefinito"):
        assert word in default, f"{word!r} is not accepted as a default word"


def test_alias_lists_are_returned_lowercased_and_deduplicated() -> None:
    for words in (cancel_aliases(), _input_aliases("ui_input_clear_aliases")):
        assert all(word == word.lower() for word in words)
        assert len(words) == len(set(words))


# --------------------------------------------------------------------------- #
# behaviour: a non-English cancel word stops the step
# --------------------------------------------------------------------------- #
def _fake_ui(lang: str) -> MagicMock:
    ui = MagicMock()
    ui.i18n = I18n(str(CATALOG_PATH))
    ui._resolve_lang = MagicMock(return_value=lang)
    ui._t = ProBotUI._t.__get__(ui, ProBotUI)
    ui._owned_rule = AsyncMock(return_value=MagicMock())
    ui._persist = AsyncMock()
    ui._render_rule_detail = MagicMock(return_value=("detail", "kbd"))
    ui._main_menu = MagicMock(return_value="menu")
    return ui


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lang", "word"),
    [
        ("en", "cancel"),
        ("fa", "لغو"),
        ("fa", "انصراف"),
        ("ru", "отмена"),
        ("zh", "取消"),
        ("fr", "annuler"),
        ("it", "annulla"),
    ],
)
async def test_cancel_word_in_any_locale_discards_the_draft(lang: str, word: str) -> None:
    ui = _fake_ui(lang)
    st = UiState()
    st.step = "rule_editing_draft"
    st.buffer = {"rule_id": "r-1"}

    message = MagicMock()
    message.from_user.id = 1234
    message.reply_text = AsyncMock()

    await ProBotUI._step_rule_editing_draft(ui, message, st, word)

    assert st.step == "", f"{word!r} ({lang}) did not cancel the step"
    assert st.buffer == {}
    assert message.reply_text.await_count == 1
    rendered = str(message.reply_text.call_args)
    assert "cancelled" in rendered.lower()


# --------------------------------------------------------------------------- #
# source: the hardcoded tuples are gone for good
# --------------------------------------------------------------------------- #
def test_no_hardcoded_alias_tuple_remains_in_the_bot_ui() -> None:
    source = UI_SOURCE.read_text(encoding="utf-8")
    for literal in ('"انصراف"', '"لغو"', '"بازگشت"', '"خروج"', '"حذف"', '"پیش‌فرض"'):
        assert literal not in source, f"hardcoded alias {literal} is back in pro_bot_ui.py"
    # …including the escaped spelling that used to keep them invisible.
    for escape in ("\\u0627\\u0646\\u0635\\u0631\\u0627\\u0641", "\\u0644\\u063a\\u0648"):
        assert escape not in source, "escaped Persian alias is back in pro_bot_ui.py"


def test_every_cancel_site_reads_the_catalog() -> None:
    source = UI_SOURCE.read_text(encoding="utf-8")
    assert source.count("cancel_aliases()") >= 20, (
        "every cancel matcher must resolve its words from the catalog"
    )
    assert '("/cancel", ' not in source
