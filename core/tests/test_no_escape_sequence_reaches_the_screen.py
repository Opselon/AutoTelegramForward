"""The escape sequence reaches the screen.

`I18n.t()` hands back the string the catalog holds — nothing between the catalog
and the Telegram client turns an escape into the character it names. So a value
written `"…\\n\\n"`, the four characters a backslash-plus-`n` twice, prints those
four characters to the user:

    ui_bl_title    '🚫 **Blacklist Keywords**\\n\\n'   ->  92, 110, 92, 110
                                                    want  10, 10

Thirty-three screens shipped that way in all ten locales — the blacklist,
whitelist, delay, header/footer, max-edit, translate, watermark, filter and
dry-run screens — so a screen composing several of them broke its lines in the
middle, in English first of all. The Python strings these values came from held
one real newline each: the escape was a transcription slip when the text moved
into the catalog, which is exactly the kind of slip a catalog-first project makes
once and then keeps making.

This file pins the class, not the instance: no catalog value may carry a C-style
escape in any locale, no string literal in the bot's own source may carry one,
and the screens that are meant to break their lines still do.
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

TELEGRAM = Path(__file__).resolve().parents[1] / "infrastructure" / "telegram"
CATALOG_PATH = TELEGRAM / "locales" / "translations.json"
SOURCE = TELEGRAM / "pro_bot_ui.py"

LOCALES = ("en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it")
SCRIPTLESS = ("fa", "ru", "zh")

#: A backslash naming a character — the text a user would read aloud as
#: "backslash n", never a line break.
ESCAPE = re.compile(r"\\[nrt]")
#: Text between backticks is a sample the user may legitimately type or paste,
#: and one of the samples is a regular expression.
CODE_SPAN = re.compile(r"`[^`]*`")

#: The screens that shipped the escape, one entry per key, kept here as the
#: ledger of what this batch repaired.
BROKEN_LINES = (
    "ui_bl_count", "ui_bl_desc", "ui_bl_more", "ui_bl_title",
    "ui_dly_current", "ui_dly_hint", "ui_dly_title",
    "ui_fl_advanced", "ui_fl_audio", "ui_fl_intro", "ui_fl_link",
    "ui_fl_sticker", "ui_fl_text", "ui_fl_title", "ui_fl_video", "ui_fl_voice",
    "ui_hf_not_set", "ui_hf_rule_line", "ui_hf_title",
    "ui_mte_current", "ui_mte_hint", "ui_mte_title",
    "ui_sim_task_line",
    "ui_tr_current", "ui_tr_hint", "ui_tr_title",
    "ui_wl_count", "ui_wl_desc", "ui_wl_more", "ui_wl_title",
    "ui_wm_current", "ui_wm_hint", "ui_wm_title",
)


def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def escapes(value: str) -> list[str]:
    """The escapes in a value, ignoring samples inside code spans."""
    return ESCAPE.findall(CODE_SPAN.sub("", value))


# --------------------------------------------------------------------------- #
# The class: an escape in the catalog
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("locale", LOCALES)
def test_no_value_in_any_locale_carries_an_escape_sequence(locale: str):
    offenders = {
        key: value for key, value in catalog()[locale].items() if escapes(value)
    }
    assert offenders == {}, f"escapes in {locale}: {offenders}"


def test_the_scan_is_not_vacuous():
    """A guard that cannot see the defect it guards against is decoration."""
    assert escapes("line one\\nline two")
    assert escapes("tab\\there")
    assert escapes("carriage\\rreturn")
    assert escapes("a real\nnewline is a newline") == []


def test_a_code_span_may_keep_its_backslash():
    """The documented exception: text the user is meant to type verbatim."""
    assert escapes("Send `\\d+` to match digits") == []
    assert escapes("A path like `C:\\notebook` is fine") == []


def test_no_source_literal_carries_an_escape_into_a_screen():
    """The same slip in Python: `"\\n🔒 AES-GCM"` is one visible line, not two.

    Only string constants that read as prose are reported, so separators
    (`"\\n".join(...)`) and regular expressions are left alone.
    """
    offenders = []
    for path in sorted(TELEGRAM.rglob("*.py")):
        if "locales" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - the file would not import
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value
            if escapes(value) and " " in value and re.search(r"[A-Za-z]{3}", value):
                offenders.append((path.name, node.lineno, value[:60]))
    assert offenders == [], f"literals that print an escape: {offenders}"


# --------------------------------------------------------------------------- #
# The screens that broke their lines
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", BROKEN_LINES)
def test_a_multi_line_screen_holds_real_line_breaks(key: str):
    value = catalog()["en"][key]
    assert escapes(value) == [], repr(value)
    assert "\n" in value, f"{key} lost its line break: {value!r}"


@pytest.mark.parametrize("key", BROKEN_LINES)
def test_every_locale_breaks_its_lines_in_the_same_places(key: str):
    """Line structure is not a translation decision: it matches English."""
    cat = catalog()
    wanted = cat["en"][key].count("\n")
    assert wanted > 0, key
    drifted = {
        locale: cat[locale][key].count("\n")
        for locale in LOCALES
        if cat[locale][key].count("\n") != wanted
    }
    assert drifted == {}, f"{key} breaks its lines elsewhere: {drifted} (en: {wanted})"


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
    ui._lang_for = lambda *sources, **kw: lang
    return ui


def make_rule() -> ForwardRule:
    rule = ForwardRule(id="r-1", name="t", source_chat_id="-1001",
                       target_chat_id="-1002", owner_user_id=7)
    rule.blacklist = ["scam", "spam"]
    rule.source_chat_name = "Source"
    return rule


@pytest.mark.parametrize("locale", LOCALES)
def test_the_blacklist_screen_reaches_the_user_in_lines(locale: str):
    """The screen that composed four of the broken values, rendered for real."""
    ui = build_ui(locale)
    text, _markup = ui._render_blacklist_menu(make_rule())

    assert "\\" not in text, f"an escape reached the {locale} screen: {text!r}"
    assert text.count("\n") >= 6, f"the {locale} screen is one run-on line: {text!r}"


# --------------------------------------------------------------------------- #
# The one literal that was not in the catalog
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("locale", LOCALES)
def test_the_backup_caption_names_the_cipher_in_the_user_s_language(locale: str):
    """`"\\n🔒 AES-GCM"` was appended to a translated sentence.

    The cipher's name is an acronym and stays; the words around it are the
    user's, and the break between the two lines is a break.
    """
    value = catalog()[locale]["ui_backup_cipher"]
    assert escapes(value) == []
    assert "AES-GCM" in value, value
    if locale in SCRIPTLESS:
        assert re.search(r"[\u0600-\u06ff\u0400-\u04ff\u4e00-\u9fff]", value), value


def test_the_caption_is_built_from_the_catalog_and_a_newline():
    text = source()
    handler = text.split("async def _step_session_backup_passphrase", 1)[-1]
    assert 'self._t("ui_backup_cipher"' in handler
    assert '"\\\\n🔒 AES-GCM"' not in text, "the escape came back"
    assert 'self._t("ui_backup_done"' in handler
