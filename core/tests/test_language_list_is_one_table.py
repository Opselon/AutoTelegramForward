"""One language list — every screen that offers a language builds it the same way.

The bot names the supported languages on three surfaces: the settings language
menu, the per-task translation picker, and the ``/lang`` list the bot manager
sends as text. A hand-written copy of that table on any one of them is how a
screen quietly keeps offering nine languages after an eleventh ships — and how
a user is shown `FA` where they should read «فارسی».

These tests hold the list to one source and the *rendering* of it to the user's
own script:

  * every supported language has an endonym and a flag, and nothing is listed
    that we do not support (a locale added to `SUPPORTED_LANGUAGES` without a
    name fails here instead of disappearing from a menu);
  * each menu offers every language exactly once, with the same label;
  * a language is named in its own words — never as a bare upper-cased code —
    on the picker, on the current-value line, on the advanced panel and in the
    confirmation toast, including a code this build no longer knows;
  * the table is not spelled out anywhere else in the module.
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

from core.domain.entities import ForwardRule
from core.infrastructure.telegram import pro_bot_ui
from core.infrastructure.telegram.i18n import SUPPORTED_LANGUAGES, I18n
from core.infrastructure.telegram.pro_bot_ui import (
    CB,
    LANG_ENDONYM,
    LANG_FLAG,
    LANG_FLAG_EMOJI,
    ProBotUI,
    language_label,
    language_row,
)

MODULE = pathlib.Path(pro_bot_ui.__file__)
OWNER = 5094837833

#: Languages whose own name is written in a script other than Latin. If one of
#: these ever reads `English`, the table was filled in from the wrong column.
NON_LATIN = ("fa", "ru", "zh")
LATIN_WITH_ITS_OWN_FORM = ("pt", "es", "fr", "it", "uz", "id")


# --------------------------------------------------------------------------- #
# A UI with no network: only the seams the renderers touch
# --------------------------------------------------------------------------- #


class FakeBot:
    def __getattr__(self, name):
        if not name.startswith("on_"):
            raise AttributeError(name)

        def register(*args, **kwargs):
            def decorator(fn):
                return fn

            return decorator

        return register


def build_ui() -> ProBotUI:
    return ProBotUI(
        bot=FakeBot(),
        i18n=I18n(),
        sessions=SimpleNamespace(),
        rules=SimpleNamespace(),
        filter_rules=SimpleNamespace(),
        ai_configs=SimpleNamespace(),
        pipeline=SimpleNamespace(),
        pool=SimpleNamespace(),
        login=SimpleNamespace(),
        log_client=None,
        admin_ids=[],
    )


def rule(**kw) -> ForwardRule:
    r = ForwardRule(
        id=1,
        name="Lab",
        source_chat_id=-1001234567890,
        target_chat_id=-1009876543210,
        owner_user_id=OWNER,
    )
    for key, value in kw.items():
        setattr(r, key, value)
    return r


def buttons(markup):
    """(label, callback_data) for every button, row by row."""
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            data = btn.callback_data
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            out.append((btn.text, data))
    return out


def ours(markup, prefix: str):
    return [(text, data) for text, data in buttons(markup) if (data or "").startswith(prefix)]


@pytest.fixture(scope="module")
def ui() -> ProBotUI:
    return build_ui()


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #


def test_every_supported_language_has_an_endonym_and_a_flag():
    assert set(LANG_ENDONYM) == set(SUPPORTED_LANGUAGES)
    assert set(LANG_FLAG_EMOJI) == set(SUPPORTED_LANGUAGES)
    for code in SUPPORTED_LANGUAGES:
        name, flag = language_row(code)
        assert name.strip(), code
        assert len(flag) == 2, f"{code} carries {flag!r}, not a flag"
        assert all("\U0001F1E6" <= ch <= "\U0001F1FF" for ch in flag), flag
        row = LANG_ENDONYM[code]
        assert row == f"{name} {flag}"
        assert LANG_FLAG[code] == row


def test_the_menu_order_is_the_canonical_order():
    """One order for every screen, so the list does not reshuffle between them."""
    assert list(LANG_FLAG) == list(SUPPORTED_LANGUAGES)


def test_each_language_is_named_in_its_own_script():
    english = language_row("en")[0]
    for code in NON_LATIN:
        name = language_row(code)[0]
        assert not name.isascii(), f"{code} reads {name!r} — that is not its own name"
        assert name != english
    for code in LATIN_WITH_ITS_OWN_FORM:
        assert language_row(code)[0] != english, code


def test_english_is_the_only_entry_equal_to_its_locale_name():
    """A Latin-script endonym must be the native form, not the English word."""
    english_names = {"pt": "Portuguese", "es": "Spanish", "fr": "French", "it": "Italian"}
    for code, english in english_names.items():
        assert language_row(code)[0].lower() != english.lower(), code


# --------------------------------------------------------------------------- #
# The labels
# --------------------------------------------------------------------------- #


def test_a_label_is_the_flag_and_the_endonym():
    assert language_label("fa") == "🇮🇷 فارسی"
    assert language_label("en") == "🇬🇧 English"
    for code in SUPPORTED_LANGUAGES:
        assert language_label(code) == f"{LANG_FLAG_EMOJI[code]} {language_row(code)[0]}"


def test_a_label_can_carry_the_code_for_a_picker():
    assert language_label("fa", with_code=True) == f"{LANG_FLAG_EMOJI['fa']} فارسی (FA)"
    assert language_label("zh", with_code=True).endswith("(ZH)")


def test_a_label_is_case_and_space_insensitive():
    assert language_label(" FA ") == language_label("fa")


def test_a_language_we_no_longer_support_is_shown_not_dropped():
    """A task pinned to a removed locale keeps a readable name on its screen."""
    assert language_label("de") == "DE"
    assert language_label("de", with_code=True) == "DE"
    assert language_label("") == ""
    assert language_label(None) == ""


# --------------------------------------------------------------------------- #
# The screens
# --------------------------------------------------------------------------- #


def test_the_settings_menu_offers_every_language_once(ui):
    seen = ours(ui._lang_menu(), CB["lang_set"] + ":")
    assert [data.split(":", 1)[1] for _, data in seen] == list(SUPPORTED_LANGUAGES)
    assert [text for text, _ in seen] == [language_label(c) for c in SUPPORTED_LANGUAGES]
    prefix = CB["lang_set"] + ":"
    other = [(t, d) for t, d in buttons(ui._lang_menu()) if not (d or "").startswith(prefix)]
    assert other, "the screen must keep a way back"


def test_the_translation_picker_offers_every_language_once(ui):
    text, markup = ui._render_translate_menu(rule())
    seen = ours(markup, "rtr_set:")
    codes = [data.rsplit(":", 1)[1] for _, data in seen]
    assert codes[0] == "none", "turning translation off comes first"
    assert codes[1:] == list(SUPPORTED_LANGUAGES)
    assert [label for label, _ in seen[1:]] == [
        language_label(c, with_code=True) for c in SUPPORTED_LANGUAGES
    ]


def test_the_picker_keeps_two_languages_per_row(ui):
    _, markup = ui._render_translate_menu(rule())
    rows = [len(row) for row in markup.inline_keyboard]
    assert rows[0] == 1 and rows[-1] == 1
    assert all(n == 2 for n in rows[1:-1]), rows


def test_the_picker_names_the_language_it_rules_on(ui):
    text, _ = ui._render_translate_menu(rule(translate_lang="fa"))
    assert "🇮🇷 فارسی (FA)" in text
    text, _ = ui._render_translate_menu(rule(translate_lang=""))
    assert "FA" not in text


def test_the_advanced_panel_names_it_too(ui):
    text, _ = ui._render_advanced_settings(rule(translate_lang="fa"))
    assert language_row("fa")[0] in text
    assert "FA:" not in text, "the panel still printed a bare code"


@pytest.mark.asyncio
async def test_the_confirmation_toast_names_it_too(ui):
    """Choosing a language answers with the language's own name, not `FA`.

    The handler is nested inside `_register` and wrapped by `_localized_handler`,
    so this pins the expression it uses rather than re-driving it: `lang_code`
    is the callback payload, and the toast must pass it through `language_label`.
    """
    source = MODULE.read_text(encoding="utf-8")
    toast = [line for line in source.splitlines() if "ui_adv_btn_translate\"" in line and "status_msg" in line]
    assert toast, "the toast line moved — find it again"
    assert "language_label(rule.translate_lang)" in toast[0]
    assert "language=language_label" in toast[0], "the toast must fill the template's {language}"
    assert "lang_code.upper()" not in source


# --------------------------------------------------------------------------- #
# The table stays in one place
# --------------------------------------------------------------------------- #


def code_strings() -> list:
    """Every string the module actually *runs*, docstrings excluded.

    `language_label`'s own docstring shows a rendered example, which is a
    description of the format, not a second copy of the list.
    """
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def test_the_module_does_not_spell_the_list_out_again():
    strings = code_strings()
    for code in SUPPORTED_LANGUAGES:
        spelled = f"({code.upper()})"
        for text in strings:
            assert spelled not in text, f"{text!r} is a second copy of the language list"
    assert "translate_lang.upper()" not in MODULE.read_text(encoding="utf-8")


def test_the_picker_builds_its_rows_from_the_table():
    source = MODULE.read_text(encoding="utf-8")
    assert "for code in SUPPORTED_LANGUAGES" in source
    for code in SUPPORTED_LANGUAGES:
        assert f'rtr_set:{{rule.id}}:{code}' not in source, f"{code} is spelled out in the picker"


# --------------------------------------------------------------------------- #
# A language is per render, never a process default
# --------------------------------------------------------------------------- #


def test_no_earlier_test_left_a_render_language_behind():
    """The canary for a language leaking out of one test and into the next.

    `_BOT_RENDER_LANGUAGE` is process-wide, so a module that sets it while
    rendering in Persian makes every later English assertion read Persian text —
    and every file still passes when run on its own. This module sorts after the
    ones that render RTL, so it is where the suite notices.
    """
    assert pro_bot_ui._BOT_RENDER_LANGUAGE.get(None) is None, (
        f"a previous test left the render language set to "
        f"{pro_bot_ui._BOT_RENDER_LANGUAGE.get(None)!r}"
    )
