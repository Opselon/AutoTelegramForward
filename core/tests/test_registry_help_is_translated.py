"""The registry panel explains a setting in the user's language, not in English.

Every `SettingSpec` that has a `help` string carries it in English — it is the
operator's schema text, and the API and the dashboard hand it out verbatim. The
bot's registry panel printed it under the setting and inside the "send the new
value" prompt, so a Persian user read a translated sentence wrapped around an
English paragraph, and the panel's search box matched only that English text.

The description is UI copy, so it lives in the catalog as `ui_reg_help_<key>`
(42 settings have one). The label half of this class is pinned by
`test_registry_labels_are_translated.py`; this file pins the description, the
prompt that repeats it, and the search haystack that now includes it.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from core.domain.entities import ForwardRule
from core.domain.rule_settings import SETTING_SPECS
from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.registry_editor import RegistryEditor

CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "infrastructure"
    / "telegram"
    / "locales"
    / "translations.json"
)
UI_DIR = Path(__file__).resolve().parents[1] / "infrastructure" / "telegram"

LOCALES = ("en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it")
PERSIAN = re.compile(r"[\u0600-\u06ff]")
CYRILLIC = re.compile(r"[\u0400-\u04ff]")
HAN = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

#: The settings whose description has to be translated.
HELP_KEYS = {key: spec.help for key, spec in SETTING_SPECS.items() if spec.help}
#: One that has no description at all: the panel must not invent one.
HELPLESS = "block_text"


class State:
    def __init__(self) -> None:
        self.step = ""
        self.buffer: dict = {}


class Saver:
    async def __call__(self, rule: ForwardRule) -> None:  # pragma: no cover
        return None


def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def translate(lang: str):
    def _t(key, **params):
        text = I18n().t(key, lang=lang, **params)
        assert text and text != key, f"{key} is missing from the {lang} catalog"
        return text

    return _t


def editor(lang: str = "fa") -> RegistryEditor:
    return RegistryEditor(translate=translate(lang), save=Saver())


def make_rule() -> ForwardRule:
    return ForwardRule(id="r-1", name="t", source_chat_id="-1001",
                       target_chat_id="-1002", owner_user_id=7)


def open_setting(ed: RegistryEditor, rule: ForwardRule, st: State, key: str) -> str:
    group = SETTING_SPECS[key].group
    ed.group(rule, st, group, 0)
    idx = ed._keys_of(st).index(key)
    text, _rows = ed.detail(rule, st, idx)
    return text


# --------------------------------------------------------------------------- #
# The catalog covers every description
# --------------------------------------------------------------------------- #


def test_the_registry_has_descriptions_to_translate():
    assert HELP_KEYS, "no setting carries help — this test would pass vacuously"
    assert HELPLESS in SETTING_SPECS and not SETTING_SPECS[HELPLESS].help


def test_every_description_has_a_catalog_key():
    cat = catalog()
    missing = sorted(k for k in HELP_KEYS if f"ui_reg_help_{k}" not in cat["en"])
    assert missing == [], f"settings with an English-only description: {missing}"


@pytest.mark.parametrize("locale", LOCALES)
def test_every_description_is_present_in_every_locale(locale: str):
    cat = catalog()
    absent = [
        k for k in HELP_KEYS
        if not str(cat.get(locale, {}).get(f"ui_reg_help_{k}", "")).strip()
    ]
    assert absent == [], f"{len(absent)} description(s) missing in {locale}: {absent[:5]}"


@pytest.mark.parametrize("locale,pattern", [
    ("fa", PERSIAN),
    ("ru", CYRILLIC),
    ("zh", HAN),
])
def test_the_descriptions_are_written_in_that_locale_s_script(locale: str, pattern):
    cat = catalog()
    untranslated = [
        k for k in HELP_KEYS
        if not pattern.search(cat[locale][f"ui_reg_help_{k}"])
    ]
    assert untranslated == [], f"descriptions not written in {locale}: {untranslated}"


def test_no_description_fell_back_to_english():
    cat = catalog()
    for locale in LOCALES:
        if locale == "en":
            continue
        same = [
            k for k in HELP_KEYS
            if cat[locale][f"ui_reg_help_{k}"] == cat["en"][f"ui_reg_help_{k}"]
        ]
        assert same == [], f"{locale} shows the English description(s) for {same}"


def test_the_translated_description_is_not_the_english_text():
    """The catalog value and `SettingSpec.help` are two different strings."""
    cat = catalog()
    for key, english in HELP_KEYS.items():
        assert cat["en"][f"ui_reg_help_{key}"] == english, (
            f"{key}: the English catalog value drifted from the registry's own text"
        )


# --------------------------------------------------------------------------- #
# The panel renders them, and only when the setting has one
# --------------------------------------------------------------------------- #


def test_opening_a_setting_explains_it_in_the_user_language():
    rule, st = make_rule(), State()
    ed = editor("fa")
    text = open_setting(ed, rule, st, "header")
    assert I18n().t("ui_reg_help_header", lang="fa") in text
    assert HELP_KEYS["header"] not in text, "the English description leaked through"


def test_the_list_prompt_repeats_the_description_in_the_user_language():
    for locale in LOCALES:
        label = I18n().t("ui_reg_set_header", lang=locale)
        help_text = I18n().t("ui_reg_help_header", lang=locale)
        text = I18n().t("ui_reg_prompt_value", lang=locale, label=label,
                        kind="str", help=help_text)
        assert help_text in text
        if locale != "en":
            assert HELP_KEYS["header"] not in text, f"{locale} kept the English text"


def test_a_setting_without_a_description_shows_none():
    """The panel explains what it can, and stays quiet about what it can't."""
    rule, st = make_rule(), State()
    ed = editor("fa")
    marker = I18n().t("ui_reg_help", lang="fa", help="X").replace("X", "").strip()
    assert marker, "the description line lost its decoration — fix this fixture"
    assert marker in open_setting(ed, rule, st, "header")
    assert marker not in open_setting(ed, rule, st, HELPLESS)


def test_every_translated_description_reaches_the_screen():
    """Walk every setting with a description, in Persian, and look for it."""
    rule, st = make_rule(), State()
    ed = editor("fa")
    for key in HELP_KEYS:
        text = open_setting(ed, rule, st, key)
        assert I18n().t(f"ui_reg_help_{key}", lang="fa") in text, key


# --------------------------------------------------------------------------- #
# A missing translation degrades loudly, never to a raw key
# --------------------------------------------------------------------------- #


def test_a_missing_description_logs_and_falls_back_to_the_english(caplog):
    ed = RegistryEditor(
        translate=lambda key, **kw: key if key == "ui_reg_help_header"
        else I18n().t(key, lang="fa", **kw),
        save=Saver(),
    )
    with caplog.at_level(logging.ERROR, logger="atf.botui.registry"):
        assert ed._help("header") == HELP_KEYS["header"]
    assert any(
        rec.levelname == "ERROR" and "ui_reg_help_header" in rec.getMessage()
        for rec in caplog.records
    )


# --------------------------------------------------------------------------- #
# Search reaches the description in the user's language
# --------------------------------------------------------------------------- #


def test_search_finds_a_setting_by_a_word_from_its_translated_description():
    rule, st = make_rule(), State()
    ed = editor("fa")
    description = I18n().t("ui_reg_help_bot_token_id", lang="fa")
    word = "رمزنگاری‌شده"
    assert word in description, "the fixture word moved — pick another from the description"
    ed.search(rule, st, word)
    assert "bot_token_id" in ed._keys_of(st), (
        f"searching for {word!r} — a word from the setting's own description — found nothing"
    )


def test_the_handler_no_longer_passes_the_english_help():
    source = (UI_DIR / "pro_bot_ui.py").read_text(encoding="utf-8")
    assert "help=(spec.help or hint)" not in source
    assert "help=(editor.help_for(key) or hint)" in source
