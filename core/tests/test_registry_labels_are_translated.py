"""The settings registry panel has to name its 72 settings in the user's language.

`setting_registry.py` (and the dashboard, and the API) describe every setting
with an English `label` and slug fallback ("clean_usernames" -> "Clean
Usernames"), and the bot's registry editor rendered exactly that: the group
list, every button, the title of each setting's screen and the "send the new
value for ..." prompt all read English in all ten locales. The key-parity
audits could not see it — nothing here is a catalog key, it is a Python string
that the UI hands to `_t` as an *argument*.

The label is UI copy, so it lives in the catalog as `ui_reg_set_<key>`. These
tests pin the two halves of that: the catalog covers every registered setting
in every locale, and the editor renders the catalog value rather than the
English slug. The last one is the search box: it matched the English label
only, so a Persian user who typed a word they could see on the button in front
of them got "nothing matches".

Add a setting server-side and the first test fails until it has ten
translations, exactly like the catalog's own parity audit.
"""
from __future__ import annotations

import asyncio
import json
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

LOCALES = ("en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it")

#: A Persian letter, so a value that fell back to English is caught.
PERSIAN = re.compile(r"[\u0600-\u06ff]")
#: Cyrillic and Han, for the same reason in the other two scripts.
CYRILLIC = re.compile(r"[\u0400-\u04ff]")
HAN = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

LABEL_KEYS = {f"ui_reg_set_{key}": key for key in SETTING_SPECS}


class State:
    """The slice of UiState the editor touches."""

    def __init__(self) -> None:
        self.step = ""
        self.buffer: dict = {}


class Saver:
    async def __call__(self, rule: ForwardRule) -> None:  # pragma: no cover
        return None


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


_CATALOG_CACHE: dict = {}
_I18N_CACHE: dict[str, I18n] = {}


def catalog() -> dict:
    """The parsed catalog, read once — json.load of the 1.5 MB table costs
    ~10 ms and this module calls it on every test's inner loop."""
    if not _CATALOG_CACHE:
        _CATALOG_CACHE.update(json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
    return _CATALOG_CACHE


def translate(lang: str):
    """The real catalog for one locale, strict about a missing key.

    A key the catalog does not carry returns the key itself, which is exactly
    the raw-key leak these tests are about — so fail here instead.
    """

    def _t(key, **params):
        inst = _I18N_CACHE.get(lang)
        if inst is None:
            inst = _I18N_CACHE[lang] = I18n()
        text = inst.t(key, lang=lang, **params)
        assert text and text != key, f"{key} is missing from the {lang} catalog"
        return text

    return _t


def editor(lang: str = "fa") -> RegistryEditor:
    return RegistryEditor(translate=translate(lang), save=Saver())


def make_rule() -> ForwardRule:
    return ForwardRule(id="r-1", name="t", source_chat_id="-1001",
                       target_chat_id="-1002", owner_user_id=7)


# --------------------------------------------------------------------------- #
# The catalog covers the registry
# --------------------------------------------------------------------------- #


def test_every_registered_setting_has_a_label_key():
    assert SETTING_SPECS, "the registry is empty — this test would pass vacuously"
    cat = catalog()
    missing = sorted(
        key for key in LABEL_KEYS.values() if f"ui_reg_set_{key}" not in cat["en"]
    )
    assert missing == [], f"settings with no catalog label: {missing}"


@pytest.mark.parametrize("locale", LOCALES)
def test_every_label_is_present_in_every_locale(locale: str):
    cat = catalog()
    absent = sorted(k for k in LABEL_KEYS if not str(cat.get(locale, {}).get(k, "")).strip())
    assert absent == [], f"{len(absent)} label(s) missing in {locale}: {absent[:5]}"


@pytest.mark.parametrize("locale", LOCALES)
def test_no_label_is_the_raw_key_or_the_english_slug(locale: str):
    cat = catalog()
    for key in LABEL_KEYS.values():
        value = cat[locale][f"ui_reg_set_{key}"]
        assert value != key, f"{key}: the {locale} label is the raw setting key"
        assert "ui_reg_set_" not in value, f"{key}: a raw key reached the {locale} label"
        assert value != key.replace("_", " ").title(), (
            f"{key}: the {locale} label is the English slug — the panel would read "
            "English for this setting"
        )


def test_the_persian_labels_are_not_english():
    cat = catalog()
    english = [k for k in LABEL_KEYS if not PERSIAN.search(cat["fa"][k])]
    assert english == [], f"labels left in English for Persian: {english}"


@pytest.mark.parametrize("locale,pattern", [
    ("fa", PERSIAN),
    ("ru", CYRILLIC),
    ("zh", HAN),
])
def test_the_labels_are_written_in_that_locale_s_script(locale: str, pattern):
    """A copy-paste that left a locale in English would still pass parity."""
    cat = catalog()
    untranslated = [k for k in LABEL_KEYS if not pattern.search(cat[locale][k])]
    assert untranslated == [], f"labels not written in {locale}: {untranslated}"


def test_no_locale_fell_back_to_english():
    """Portuguese and Spanish legitimately share a few words; English is not."""
    cat = catalog()
    for locale in LOCALES:
        if locale == "en":
            continue
        same = [k for k in LABEL_KEYS if cat[locale][k] == cat["en"][k]]
        assert same == [], f"{locale} shows the English label(s) for {same}"


# --------------------------------------------------------------------------- #
# The editor renders those, and never the English
# --------------------------------------------------------------------------- #


def _labels_seen(ed: RegistryEditor, rule: ForwardRule, st: State) -> list:
    """Every setting label the panel shows, walking the pages a user would."""
    seen = []
    for group, keys in ed._groups():
        page = 0
        while True:
            text, rows = ed.group(rule, st, group, page)
            seen += [row[0][0] for row in rows if str(row[0][1]).startswith("rge:k:")]
            assert "Clean Usernames" not in text
            following = f"rge:G:{rule.id}:{group}:{page + 1}"
            if not any(str(btn[1]) == following for row in rows for btn in row):
                break
            page += 1
    return seen


def test_the_group_list_renders_the_catalog_label_in_every_locale():
    rule = make_rule()
    st = State()
    for lang in LOCALES:
        ed = editor(lang)
        _, rows = ed.groups(rule, st)
        assert [row[0][0] for row in rows], f"the group list is empty in {lang}"
        seen = _labels_seen(ed, rule, st)
        assert len(seen) == len(SETTING_SPECS), (
            f"{lang}: the panel listed {len(seen)} of {len(SETTING_SPECS)} settings"
        )
        if lang == "fa":
            assert all(PERSIAN.search(label) for label in seen), (
                "a Persian panel rendered an English setting name: "
                f"{[x for x in seen if not PERSIAN.search(x)][:5]}"
            )
        elif lang != "en":
            cat = catalog()
            english_only = [
                label for label in seen if label in {cat["en"][k] for k in LABEL_KEYS}
            ]
            assert english_only == [], f"{lang} panel showed the English label(s): {english_only}"


def test_opening_a_setting_titles_it_in_the_user_language():
    rule = make_rule()
    st = State()
    ed = editor("fa")
    group = SETTING_SPECS["remove_emojis"].group
    ed.group(rule, st, group, 0)  # sets the page the taps refer to
    idx = ed._keys_of(st).index("remove_emojis")
    text, _ = ed.detail(rule, st, idx)
    assert I18n().t("ui_reg_set_remove_emojis", lang="fa") in text
    assert "Remove emojis" not in text


def test_the_value_prompt_asks_for_the_setting_by_its_translated_name():
    """The prompt is built in the handler, so pin it at the source too."""
    source = (
        Path(__file__).resolve().parents[1]
        / "infrastructure"
        / "telegram"
        / "pro_bot_ui.py"
    ).read_text(encoding="utf-8")
    assert "label=spec.label or key" not in source, (
        "the value prompt still names the setting with the raw key when the "
        "registry has no English label"
    )
    assert "label=editor.label_for(key)" in source


# --------------------------------------------------------------------------- #
# Search in the user's language
# --------------------------------------------------------------------------- #


def test_search_finds_a_setting_by_its_translated_name():
    rule = make_rule()
    st = State()
    ed = editor("fa")
    word = I18n().t("ui_reg_set_remove_emojis", lang="fa").split()[1]  # the object, not the verb
    text, rows = ed.search(rule, st, word)
    keys = ed._keys_of(st)
    assert "remove_emojis" in keys, (
        f"searching for {word!r} — a word the panel itself shows — found nothing"
    )
    assert I18n().t("ui_reg_set_remove_emojis", lang="fa") in text


def test_the_search_still_answers_the_technical_key():
    rule = make_rule()
    st = State()
    ed = editor("fa")
    ed.search(rule, st, "remove_emojis")
    assert ed._keys_of(st) == ["remove_emojis"]
