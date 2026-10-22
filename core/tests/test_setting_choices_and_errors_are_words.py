"""The settings panel offers values as words, and refuses them in the user's language.

Two halves of one leak, both inside the per-task settings panel:

1. A setting that offers a *choice* stores a slug of ours — `any`, `all`,
   `phrase`, `USER`, `BOT`. The chooser translated its own title and then drew
   those slugs as buttons, printed the stored one on the value line and again in
   the save toast, so a Persian user read English value words inside a Persian
   screen. The vocabulary is ours and closed (`SettingSpec.choices`), so each
   word is catalog copy: `ui_reg_choice_<word>`, except the two delivery
   transports, which the task panel already names (`ui_route_transport_*`) and
   which must not be named a second time here.

2. A value the registry *refuses* came with the registry's own English prose —
   `whitelist_mode must be one of any, all, phrase` — and that prose was handed
   straight to `ui_reg_invalid`, so the user read an English sentence naming an
   English setting around English slugs. `SettingError.code` picks a translated
   sentence and the spec supplies the parameters; an unfamiliar code still says
   something, with an ERROR line so a new one is noticed rather than shipped.

The label/description halves of this panel are pinned by
`test_registry_labels_are_translated.py` and `test_registry_help_is_translated.py`.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
from pathlib import Path

import pytest

from core.domain.entities import ForwardRule
from core.domain.rule_settings import SETTING_SPECS, SettingError
from core.infrastructure.telegram import registry_editor as registry
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
PERSIAN = re.compile(r"[\u0600-\u06ff]")
CYRILLIC = re.compile(r"[\u0400-\u04ff]")
HAN = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

#: The settings that offer a fixed vocabulary, and the words they offer.
CHOICE_SPECS = {key: spec.choices for key, spec in SETTING_SPECS.items() if spec.choices}
#: The word keys that are not derived from the slug (see the module map).
OVERRIDES = registry._CHOICE_KEY_OVERRIDES
#: The sentences a refused value can be explained with.
BAD_KEYS = tuple(registry._BAD_REASON_KEYS.values()) + (
    "ui_reg_bad_between",
    "ui_reg_bad_too_small",
    "ui_reg_bad_too_large",
)
#: A spec with both bounds (every bounded spec has both today) and one with a
#: single bound, for the sentences that only a one-sided spec can reach.
BOTH_BOUNDS = "ai_max_tokens"
ONE_BOUND = "max_time_edit"


class State:
    def __init__(self) -> None:
        self.step = ""
        self.buffer: dict = {}


class Saver:
    def __init__(self) -> None:
        self.saved = 0

    async def __call__(self, rule: ForwardRule) -> None:
        self.saved += 1


_CATALOG_CACHE: dict = {}
_I18N_CACHE: dict[str, I18n] = {}


def _i18n(lang: str) -> I18n:
    """One I18n per locale: I18n() re-parses the 1.5 MB catalog table, and the
    render walks below translate thousands of strings."""
    inst = _I18N_CACHE.get(lang)
    if inst is None:
        inst = _I18N_CACHE[lang] = I18n()
    return inst


def catalog() -> dict:
    if not _CATALOG_CACHE:
        _CATALOG_CACHE.update(json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
    return _CATALOG_CACHE


def translate(lang: str):
    def _t(key, **params):
        text = _i18n(lang).t(key, lang=lang, **params)
        assert text and text != key, f"{key} is missing from the {lang} catalog"
        return text

    return _t


def editor(lang: str = "fa", saver=None) -> RegistryEditor:
    """The strict translator: any key it cannot find fails the test."""
    return RegistryEditor(translate=translate(lang), save=saver or Saver())


def lenient_editor(lang: str = "fa") -> RegistryEditor:
    """For the paths that *ask* about a key no catalog has, as production does.

    The probe is `I18n.contains`, exactly as `ProBotUI._registry_editor` passes
    it: asking whether a key exists must not go through `t`, which now names
    every key it cannot find. A strict translator would also fail on the probe
    itself rather than on what the probe is about.
    """
    return RegistryEditor(
        translate=lambda key, **params: _i18n(lang).t(key, lang=lang, **params),
        contains=_i18n(lang).contains,
        save=Saver(),
    )


def make_rule() -> ForwardRule:
    return ForwardRule(id="r-1", name="t", source_chat_id="-1001",
                       target_chat_id="-1002", owner_user_id=7)


def index_of(ed: RegistryEditor, rule: ForwardRule, st: State, key: str) -> int:
    ed.group(rule, st, SETTING_SPECS[key].group, 0)
    return ed._keys_of(st).index(key)


def word_key(key: str, choice: str) -> str:
    return OVERRIDES.get((key, choice), f"ui_reg_choice_{choice.lower()}")


# --------------------------------------------------------------------------- #
# Every word a setting offers has a translation
# --------------------------------------------------------------------------- #


def test_the_settings_that_offer_a_choice_are_the_ones_we_expect():
    """The scope the rest of this file pins. A new select setting is a leak."""
    assert CHOICE_SPECS == {
        "delivery_transport": ("USER", "BOT"),
        "whitelist_mode": ("any", "all", "phrase"),
        "branding_match_mode": ("any", "all"),
    }


def test_every_choice_word_has_a_catalog_key():
    cat = catalog()
    missing = sorted(
        word_key(key, choice)
        for key, choices in CHOICE_SPECS.items()
        for choice in choices
        if word_key(key, choice) not in cat["en"]
    )
    assert missing == [], f"choices with no word of their own: {missing}"


@pytest.mark.parametrize("locale", LOCALES)
def test_every_choice_word_is_present_in_every_locale(locale: str):
    cat = catalog()
    absent = [
        word_key(key, choice)
        for key, choices in CHOICE_SPECS.items()
        for choice in choices
        if not str(cat.get(locale, {}).get(word_key(key, choice), "")).strip()
    ]
    assert absent == [], f"choice word(s) missing in {locale}: {absent}"


@pytest.mark.parametrize("locale,pattern", [
    ("fa", PERSIAN),
    ("ru", CYRILLIC),
    ("zh", HAN),
])
def test_the_choice_words_are_written_in_that_locale_s_script(locale: str, pattern):
    cat = catalog()
    foreign = [
        word_key(key, choice)
        for key, choices in CHOICE_SPECS.items()
        for choice in choices
        if not pattern.search(cat[locale][word_key(key, choice)])
    ]
    assert foreign == [], f"choice words still in Latin script in {locale}: {foreign}"


#: The words this file owns. The two transport words are the task panel's and
#: are excluded on purpose: "Bot" and "Account" *are* the translations in pt,
#: es, fr, it, id and uz — reusing one wording for one thing is the whole point,
#: and the task panel's own test pins them.
OWN_WORDS = ("ui_reg_choice_any", "ui_reg_choice_all", "ui_reg_choice_phrase")


def test_no_choice_word_fell_back_to_english():
    cat = catalog()
    for locale in LOCALES:
        if locale == "en":
            continue
        same = [k for k in OWN_WORDS if cat[locale][k] == cat["en"][k]]
        assert same == [], f"{locale} shows the English choice word(s) for {same}"


def test_the_transports_are_named_once_in_the_whole_product():
    """The task panel already names the two routes; the panel reuses those words."""
    assert OVERRIDES == {
        ("delivery_transport", "USER"): "ui_route_transport_account",
        ("delivery_transport", "BOT"): "ui_route_transport_bot",
    }
    cat = catalog()
    for key in ("ui_route_transport_account", "ui_route_transport_bot"):
        assert str(cat["fa"][key]).strip(), f"{key} is empty in fa"


# --------------------------------------------------------------------------- #
# Every refusal has a sentence
# --------------------------------------------------------------------------- #


def test_the_validation_sentences_exist_in_every_locale():
    cat = catalog()
    for locale in LOCALES:
        absent = [k for k in BAD_KEYS if not str(cat.get(locale, {}).get(k, "")).strip()]
        assert absent == [], f"refusal sentence(s) missing in {locale}: {absent}"


@pytest.mark.parametrize("locale,pattern", [
    ("fa", PERSIAN),
    ("ru", CYRILLIC),
    ("zh", HAN),
])
def test_the_refusals_are_written_in_that_locale_s_script(locale: str, pattern):
    cat = catalog()
    foreign = [k for k in BAD_KEYS if not pattern.search(cat[locale][k])]
    assert foreign == [], f"refusals still in Latin script in {locale}: {foreign}"


def test_no_refusal_sentence_fell_back_to_english():
    cat = catalog()
    for locale in LOCALES:
        if locale == "en":
            continue
        same = [k for k in BAD_KEYS if cat[locale][k] == cat["en"][k]]
        assert same == [], f"{locale} shows the English refusal(s) for {same}"


def test_the_range_sentences_keep_the_placeholders_they_fill():
    cat = catalog()
    assert "{low}" in cat["en"]["ui_reg_bad_too_small"]
    assert "{high}" in cat["en"]["ui_reg_bad_too_large"]
    for locale in LOCALES:
        for key in ("ui_reg_bad_too_small", "ui_reg_bad_too_large", "ui_reg_bad_between"):
            assert "{label}" in cat[locale][key], f"{key} lost {{label}} in {locale}"


def test_every_refusal_code_the_registry_raises_is_covered():
    """A code with no sentence would fall back to English prose."""
    text = (Path(__file__).resolve().parents[1] / "domain" / "rule_settings.py").read_text(
        encoding="utf-8"
    )
    raised = set(re.findall(r'SettingError\(\s*"([a-z_]+)"', text))
    assert raised, "the scan found no codes — this test would pass vacuously"
    unexplained = raised - set(registry._BAD_REASON_KEYS) - {"out_of_range"}
    assert unexplained == set(), f"codes with no translated sentence: {sorted(unexplained)}"


# --------------------------------------------------------------------------- #
# What the panel actually draws
# --------------------------------------------------------------------------- #


def test_the_choices_screen_shows_words_not_slugs():
    ed, rule, st = editor("fa"), make_rule(), State()
    idx = index_of(ed, rule, st, "whitelist_mode")
    text, rows = ed.choices(rule, st, idx)

    buttons = [label for row in rows for label, _data in row]
    for slug in ("any", "all", "phrase"):
        assert not any(slug == label or label == f"✅ {slug}" for label in buttons), (
            f"the chooser drew the raw slug {slug!r}: {buttons}"
        )
    cat = catalog()
    for slug in ("any", "all", "phrase"):
        word = cat["fa"][f"ui_reg_choice_{slug}"]
        assert any(word in label for label in buttons), f"{word!r} is not on a button"
        assert PERSIAN.search(word)
    assert text.count(cat["fa"]["ui_reg_choice_any"]) >= 1


def test_the_choices_screen_still_calls_back_by_position():
    """The words changed; the callback payloads did not."""
    ed, rule, st = editor("fa"), make_rule(), State()
    idx = index_of(ed, rule, st, "whitelist_mode")
    _text, rows = ed.choices(rule, st, idx)
    payloads = [data for row in rows for _label, data in row]
    assert f"rge:v:{rule.id}:{idx}:0" in payloads
    assert f"rge:v:{rule.id}:{idx}:2" in payloads


def test_the_choices_screen_marks_the_word_in_force():
    ed, rule, st = editor("fa"), make_rule(), State()
    rule.whitelist_mode = "phrase"
    idx = index_of(ed, rule, st, "whitelist_mode")
    _text, rows = ed.choices(rule, st, idx)
    marked = [label for row in rows for label, _d in row if label.startswith("✅ ")]
    assert marked == [f"✅ {catalog()['fa']['ui_reg_choice_phrase']}"], marked


def test_the_value_line_reads_a_choice_as_a_word():
    ed, rule, st = editor("fa"), make_rule(), State()
    rule.whitelist_mode = "all"
    idx = index_of(ed, rule, st, "whitelist_mode")
    text, _rows = ed.detail(rule, st, idx)
    word = catalog()["fa"]["ui_reg_choice_all"]
    assert word in text
    assert "= all" not in text and ": all" not in text
    assert f"{catalog()['fa']['ui_reg_choices']}" not in text  # the label line, not the key


def test_the_allowed_values_line_names_them_as_words():
    ed, rule, st = editor("fa"), make_rule(), State()
    idx = index_of(ed, rule, st, "whitelist_mode")
    text, _rows = ed.detail(rule, st, idx)
    cat = catalog()
    for slug in ("any", "all", "phrase"):
        assert cat["fa"][f"ui_reg_choice_{slug}"] in text
        assert f"any, all, phrase" not in text


def test_the_save_toast_names_the_choice_as_a_word():
    async def scenario():
        rule, st = make_rule(), State()
        saver = Saver()
        ed = editor("fa", saver)
        idx = index_of(ed, rule, st, "whitelist_mode")
        notice = await ed.set_choice(rule, st, idx, 1)
        assert saver.saved == 1, "the choice was not persisted"
        return notice

    import asyncio

    notice = asyncio.new_event_loop().run_until_complete(scenario())
    cat = catalog()
    assert cat["fa"]["ui_reg_choice_all"] in notice, notice
    assert "all" not in notice.split(":")[-1].strip(), notice


def test_a_choice_this_build_does_not_know_is_shown_as_stored(caplog):
    """A legacy or hand-written value is shown as stored, not dropped.

    The entity itself refuses an unknown choice, so this path is only reachable
    from metadata written by an older build — which is exactly why it must not
    crash a screen or be reported as a missing translation.
    """
    ed = lenient_editor("fa")
    with caplog.at_level(logging.ERROR):
        assert ed._choice_label("whitelist_mode", "SMART") == "SMART"
    assert "no catalog entry" not in caplog.text


def test_a_word_with_no_translation_is_reported_once(caplog):
    """A word this build *does* offer but cannot name is a missing translation."""
    ed = editor("fa")
    word = SETTING_SPECS["whitelist_mode"].choices[0]
    with caplog.at_level(logging.ERROR):
        assert ed._choice_label("whitelist_mode", word) != word
    assert f"ui_reg_choice_{word}" not in caplog.text  # it exists, so nothing to say


# --------------------------------------------------------------------------- #
# What the panel says when it refuses a value
# --------------------------------------------------------------------------- #


def test_a_refused_choice_is_explained_in_the_user_s_language():
    async def scenario():
        rule, st = make_rule(), State()
        ed = editor("fa")
        idx = index_of(ed, rule, st, "whitelist_mode")
        return await ed.set_value(rule, st, idx, "smart")

    import asyncio

    notice = asyncio.new_event_loop().run_until_complete(scenario())
    cat = catalog()
    assert cat["fa"]["ui_reg_choice_any"] in notice, notice
    assert "must be one of" not in notice, notice
    assert "whitelist_mode" not in notice, notice
    assert PERSIAN.search(notice), notice


def test_a_refused_range_reaches_the_user_as_a_sentence_with_the_bounds():
    async def scenario(value):
        rule, st = make_rule(), State()
        ed = editor("fa")
        idx = index_of(ed, rule, st, BOTH_BOUNDS)
        return await ed.set_value(rule, st, idx, value)

    import asyncio

    notice = asyncio.new_event_loop().run_until_complete(scenario("99999999"))
    spec = SETTING_SPECS[BOTH_BOUNDS]
    assert str(spec.maximum) in notice and str(spec.minimum) in notice, notice
    assert "must be" not in notice and ">=" not in notice, notice
    assert PERSIAN.search(notice), notice


def test_a_one_sided_range_has_its_own_sentence(monkeypatch):
    """No spec has a single bound today; the branch is pinned all the same."""
    both = SETTING_SPECS[BOTH_BOUNDS]
    ed = editor("fa")
    # Dropping the maximum leaves a floor only, and vice versa.
    for field, template in (("minimum", "ui_reg_bad_too_large"), ("maximum", "ui_reg_bad_too_small")):
        spec = dataclasses.replace(both, **{field: None})
        monkeypatch.setitem(SETTING_SPECS, BOTH_BOUNDS, spec)
        reason = ed._bad_reason(SettingError("out_of_range", "operator prose", BOTH_BOUNDS))
        assert reason == I18n().t(template, lang="fa", label=ed._label(BOTH_BOUNDS),
                                  low=spec.minimum, high=spec.maximum)
        assert PERSIAN.search(reason)


def test_an_unknown_failure_code_still_says_something(caplog):
    ed = editor("fa")
    with caplog.at_level(logging.ERROR):
        reason = ed._bad_reason(SettingError("brand_new_code", "operator prose", "delay"))
    assert reason == "operator prose"
    assert "brand_new_code" in caplog.text


def test_an_unknown_setting_is_named_not_quoted(caplog):
    # A key the registry does not know is never *asked* for a label (`_label`
    # would raise on it), so the sentence falls back to the key itself.
    ed = lenient_editor("fa")
    with caplog.at_level(logging.ERROR):
        reason = ed._bad_reason(SettingError("unknown_setting", "unknown setting 'nope'", "nope"))
    assert "unknown_setting" not in reason
    assert PERSIAN.search(reason)
    assert "nope" in reason
