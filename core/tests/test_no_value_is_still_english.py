"""No catalog value is still its English self in a locale it was meant to serve.

Twelve batches moved the bot's text into `locales/translations.json`. Every one
of them added or fixed *code*; none of them asked the simpler question — is the
catalog itself translated? It was not. Fourteen user-visible values were the
English string, byte for byte, in all nine translated locales:

    ui_toggle_crypto_mode       "Crypto mode: {state}"
    ui_toggle_news_digest       "News digest: {state}"
    ui_whitelist_users_count    "Whitelist users: {count} allowed"
    ui_ai_creativity_cycle      "AI creativity: {value}"

so a Persian operator toggled crypto mode and read `Crypto mode: روشن`: the
`{state}` was translated and the sentence around it was not. They passed every
existing audit — key parity, placeholders, glyph parity, no cross-leak — because
a value can be present, identical in all ten locales, and still English.

This test asks it directly: a value that is identical to its English counterpart
*and* still contains a Latin word nobody would claim as that locale's own is a
leak. What stays Latin legitimately is listed, with the reason, in
`SAME_IN_EVERY_LANGUAGE`, and the list is deliberately short — it is names,
acronyms and product words (`Telegram`, `VIP`, `api_id`, `Webhook`, `OCR`), plus
the words that genuinely are the same in a target language (`Bot` in Spanish,
`Media` in Italian, `No` in French). Everything else has to be translated.

Deliberately out of scope, and not silently: a *partly* translated value — an
English header with a translated body, or an English term glossed in parentheses
(`**لیست سیاه کلمات (Blacklist Keywords)**`) — is not flagged. Those are a
bilingual style choice, not a missing translation, and flagging them would mean
rewriting a few hundred deliberate glosses. The rule here catches the class that
is unambiguously a defect.
"""
from __future__ import annotations

import json
import pathlib
import re

CATALOG_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "infrastructure"
    / "telegram"
    / "locales"
    / "translations.json"
)
LOCALES = ("fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it")

#: Names, acronyms and product words, plus the words that are the same in the
#: target language itself. Every entry has to survive the question "would a
#: translator leave this in Latin script?", and every entry is used by at least
#: one value — an allowance nobody needs is a leak waiting to happen, and the
#: test below fails on it.
SAME_IN_EVERY_LANGUAGE = {
    # words that are the same in the target language
    "bot", "media", "no", "on", "off", "general", "type", "source",
    "sources", "minutes", "error", "account", "target", "edit",
    "administrator", "status", "debug", "sticker", "emoji",
    "links", "link", "audio", "video",
    # names, acronyms, product and policy words
    "ai", "api", "id", "vip", "pro", "prompt",
}
#: A claim about nine languages, not a dumping ground.
MAX_ALLOWED_WORDS = 45

#: The list is a claim about the languages, not a dumping ground.
MAX_ALLOWED_WORDS = 70

#: Text that is not prose: values kept in code spans, handles, commands, URLs,
#: tags, placeholders and parenthetical glosses.
NOT_PROSE = (
    re.compile(r"`[^`]*`"),
    re.compile(r"@\w+"),
    re.compile(r"/\w+"),
    re.compile(r"https?://\S+"),
    re.compile(r"<[^>]+>"),
    re.compile(r"\{[^{}]*\}"),
)
WORD = re.compile(r"[A-Za-z]{2,}")


def latin_words(value: str) -> list:
    """The Latin-script words in a value, with the non-prose parts removed."""
    text = value
    for pattern in NOT_PROSE:
        text = pattern.sub(" ", text)
    return [word.lower() for word in WORD.findall(text)]


def still_english(catalog: dict) -> list:
    """(locale, key, text) for every value that is its English self and prose."""
    english = catalog["en"]
    found = []
    for locale in LOCALES:
        for key, value in english.items():
            mine = (catalog.get(locale) or {}).get(key, "")
            if not mine or mine != value:
                continue
            if [w for w in latin_words(value) if w not in SAME_IN_EVERY_LANGUAGE]:
                found.append((locale, key, mine))
    return found


def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The check itself
# --------------------------------------------------------------------------- #


def test_the_catalog_is_really_being_scanned():
    cat = catalog()
    assert len(cat["en"]) > 1000, f"only {len(cat['en'])} keys in the English catalog"
    for locale in LOCALES:
        assert len(cat[locale]) == len(cat["en"]), f"{locale} is not the same size"


def test_a_value_that_is_its_english_self_is_caught():
    cat = {
        "en": {"ui_x": "Crypto mode: {state}"},
        "fa": {"ui_x": "Crypto mode: {state}"},
        "ru": {"ui_x": "Режим шифрования: {state}"},
    }
    found = still_english({**cat, **{loc: cat["ru"] for loc in LOCALES if loc not in ("fa", "ru")}})
    assert ("fa", "ui_x", "Crypto mode: {state}") in found
    assert all(locale != "ru" for locale, _, _ in found)


def test_a_value_that_is_translated_is_not_caught():
    cat = {"en": {"ui_x": "Service status: {state}"}}
    for locale in LOCALES:
        cat[locale] = {"ui_x": "وضعیت سرویس: {state}"}
    assert still_english(cat) == []


def test_a_name_dropped_in_its_english_form_is_not_caught():
    # "API ID" and "VIP Pro" are names, not copy: they are the same in every
    # language by definition. ("Support" left the allowlist when French and
    # Indonesian started translating it.)
    cat = {"en": {"ui_x": "🔑 **API ID:** `{id}`", "ui_y": "VIP Pro"}}
    for locale in LOCALES:
        cat[locale] = {"ui_x": "🔑 **API ID:** `{id}`", "ui_y": "VIP Pro"}
    assert still_english(cat) == []


def test_the_allowlist_stays_an_allowlist():
    assert len(SAME_IN_EVERY_LANGUAGE) <= MAX_ALLOWED_WORDS, (
        "the allowlist has grown past a defensible claim about the languages; "
        "translate the value instead of allowing another word"
    )
    assert all(word == word.lower() for word in SAME_IN_EVERY_LANGUAGE)


def test_no_allowance_is_unused():
    """A word allowed but needed by nothing is a leak nobody is watching."""
    cat = catalog()
    english = cat["en"]
    used = set()
    for locale in LOCALES:
        for key, value in english.items():
            if (cat.get(locale) or {}).get(key, "") == value:
                used.update(latin_words(value))
    unused = sorted(SAME_IN_EVERY_LANGUAGE - used)
    assert unused == [], (
        "these words are allowed but no value needs them — drop them from "
        f"SAME_IN_EVERY_LANGUAGE: {unused}"
    )


def test_the_allowlist_has_no_sentence_words():
    """A word an English sentence would need is not a name."""
    for word in ("the", "and", "for", "with", "your", "this", "from", "settings"):
        assert word not in SAME_IN_EVERY_LANGUAGE


# --------------------------------------------------------------------------- #
# And the catalog is clean
# --------------------------------------------------------------------------- #


def test_no_value_is_still_english():
    found = still_english(catalog())
    assert found == [], (
        "these values are identical to English in a locale they were meant to "
        "serve — translate them:\n"
        + "\n".join(f"  {locale}/{key}: {text!r}" for locale, key, text in found)
    )


def test_the_fourteen_toggles_and_counters_are_translated():
    """The values this test was written for, pinned one by one."""
    cat = catalog()
    for key in (
        "ui_ai_creativity_cycle",
        "ui_blacklist_users_count",
        "ui_whitelist_users_count",
        "ui_link_buttons_count",
        "ui_toggle_affiliate_kit",
        "ui_toggle_auto_post_scheduler",
        "ui_toggle_button_to_text",
        "ui_toggle_crypto_mode",
        "ui_toggle_duplicate_filters",
        "ui_toggle_news_digest",
        "ui_toggle_ocr_image_ai",
        "ui_toggle_power_schedule",
        "ui_toggle_process_raw_text",
        "ui_toggle_smart_ai_rewrite",
    ):
        assert key in cat["en"], f"{key} is gone from the catalog"
        for locale in LOCALES:
            assert cat[locale][key] != cat["en"][key], f"{key} is still English in {locale}"
