"""No English phrase is left standing inside a translated sentence.

Batch 13 asked whether a value is *identical* to its English self. This asks the
next question, for the three locales that do not use the Latin script: is an
English phrase standing *inside* a value whose sentence was translated?

    ru: '🩺 **Health Check:** 🔴 **Ошибка подключения**'
    fa: '🛡 … در صورت Restricted بودن فوروارد، دانلود و Clean Copy …'
    fa: '… با رعایت Rate Limit ارسال خواهد شد.'
    zh: '🌐 **主机 / Base URL：** `{base_url}`'

Each passed every audit the repository has, because a sentence can be present,
translated, complete, and still have an untranslated term in the middle of it.
Thirty-two values across nine locales carried one — status headers, policy terms,
rate-limit phrasing, credential field names.

The rule: for `fa`, `ru` and `zh`, a run of two or more Latin words that is not a
name, not an identifier and not a term of art is a leak. Both allowances are
listed below and both are audited — a word allowed but needed by no value fails
`test_every_allowance_is_used`, so the lists cannot quietly grow.

Honest limit, stated rather than hidden: this scan cannot see the same defect in
the six Latin-script locales, where "Base URL" or "Fail Closed" is invisible to a
script test. Those eight terms were found by reading the catalog and are pinned
in `RETIRED_ENGLISH` below — every one must appear in the English catalog (proof
it is a real English term we retired) and in none of the nine translated locales.
"""
from __future__ import annotations

import json
import pathlib
import re
import unicodedata

CATALOG_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "infrastructure"
    / "telegram"
    / "locales"
    / "translations.json"
)

#: Locales whose script tells a Latin run apart from their own writing. The last
#: test checks that these really are the non-Latin ones, so the list cannot be
#: trimmed to make the scan pass.
NON_LATIN = ("fa", "ru", "zh")

#: Terms of art we do keep in Latin script, with the value that needs each one.
TERMS = frozenset(
    {
        "ai",  # AI, our own short form in every locale
        "api",  # API (ID, key, hash) — the credential field names
        "gateway",  # Web Gateway — the name of our ingest service
        "id",  # identifier
        "ocr",  # OCR
        "pro",  # VIP Pro — the plan name
        "telegram",  # the platform
        "vip",  # VIP — the plan name
        "web",  # Web dashboard — the name of our panel
    }
)

#: Product names. A name is not a phrase to translate.
PHRASES = frozenset({"auto forward", "autoforward pro"})

#: English terms this batch retired, each with the value that leaked it. Every
#: entry must still be present in the English catalog, and absent from every
#: locale we translate — including the six where the script scan is blind.
RETIRED_ENGLISH = (
    "API Hash",  # ui_client_credentials_api_hash, ui_step_api_hash_prompt
    "API Key",  # ui_ai_detail_key
    "API keys",  # err_api_id_invalid
    "Base URL",  # ui_ai_detail_baseurl
    "Clean Copy",  # ui_smart_lock_policy, ui_sim_step_c1
    "Fail Closed",  # ui_ai_policy_short_drop
    "Fail Open",  # ui_ai_policy_short_send_original
    "Health Check",  # ui_ai_health_fail, ui_ai_health_ok
    "Idempotency Key",  # ui_smart_antiloop
    "Re-upload",  # ui_smart_lock_policy, ui_sim_step_c1
    "Telegram User ID",  # ui_admin_add_prompt
)

#: What to blank out before looking for English. Code spans, placeholders,
#: handles, URLs, identifiers and marked-up emphasis are not prose; a gloss in
#: parentheses is a deliberate bilingual style choice, not a missing translation.
_NOT_PROSE = (
    re.compile(r"`[^`]*`"),
    re.compile(r"\{[^{}]*\}"),
    re.compile(r"@\w+"),
    re.compile(r"https?://\S+"),
    re.compile(r"<[^>]+>"),
    re.compile(r"\*\*"),
    re.compile(r"\([^()]*\)"),
    re.compile(r"（[^（）]*）"),
    re.compile(r"\[[^\[\]]*\]"),
    # an identifier: api_id, auto_post_scheduler.enabled, api_hash
    re.compile(r"[A-Za-z][A-Za-z0-9]*[_.][A-Za-z0-9_.]*"),
)

#: Two or more Latin words with nothing but spaces between them.
_RUN = re.compile(r"[A-Za-z][A-Za-z'’-]{1,}(?:[ \t]+[A-Za-z][A-Za-z'’-]{1,})*")

_LATIN_LETTER = re.compile(r"[A-Za-z]")
_ANY_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def catalog() -> dict[str, dict[str, str]]:
    with CATALOG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def latin_runs(value: str) -> list[str]:
    """Every run of two or more Latin words that is prose, not a name or term."""
    text = value
    for pattern in _NOT_PROSE:
        text = pattern.sub(" ", text)
    found = []
    for match in _RUN.finditer(text):
        run = match.group(0).strip()
        words = run.split()
        if len(words) < 2:
            continue
        if run.lower() in PHRASES:
            continue
        if all(word.lower().strip("-'’") in TERMS for word in words):
            continue
        found.append(run)
    return found


def leaks(values: dict[str, str]) -> list[tuple[str, str]]:
    """(key, the English run it carries) for one locale."""
    return [(key, run) for key, value in values.items() for run in latin_runs(value)]


def latin_share(value: str) -> float:
    """How much of the value's letters are Latin script, 0.0 to 1.0."""
    letters = _ANY_LETTER.findall(value)
    if not letters:
        return 0.0
    return sum(bool(_LATIN_LETTER.match(letter)) for letter in letters) / len(letters)


# --------------------------------------------------------------------------- #
# the scan
# --------------------------------------------------------------------------- #
def test_no_english_phrase_survives_inside_a_translated_sentence():
    data = catalog()
    offenders = {locale: leaks(data[locale]) for locale in NON_LATIN}
    offenders = {locale: found for locale, found in offenders.items() if found}
    assert offenders == {}, (
        "an English phrase is standing inside a translated value; translate the "
        f"phrase, or say why it is a name: {offenders}"
    )


def test_the_scan_sees_the_leaks_it_was_written_for():
    """The four shapes that shipped, each still caught after the fix."""
    for value in (
        "🩺 **Health Check:** 🔴 **Ошибка подключения**",
        "🛡 در صورت Restricted بودن فوروارد، دانلود و Clean Copy انجام می‌شود.",
        "⚠️ ارسال پیام در پس‌زمینه با رعایت Rate Limit انجام می‌شود.",
        "🌐 **主机 / Base URL：** `{base_url}`",
        "🔒 فعال با بررسی Idempotency Key و منشأ پیام",
    ):
        assert latin_runs(value), f"the scan missed a leak: {value!r}"


def test_a_term_of_art_is_not_a_leak():
    """The words we deliberately keep, in the values that keep them."""
    for value in (
        "🔑 **API ID:** `{api_id}`",  # api, id
        "📱 **Telegram API**",  # telegram, api
        "🎖 **VIP Pro**",  # vip, pro
        "🔍 **OCR AI**",  # ocr, ai
        "🖥 Web Gateway: 🟢",  # web, gateway
        "ربات **Auto Forward** به شما کمک می‌کند",  # product name
        "👋 به **AutoForward Pro** خوش آمدید!",  # product name
        "🔑 **کلید احراز هویت (API Key):** `{masked_key}`",  # a gloss in parentheses
        "نمای میانبر از auto_post_scheduler.enabled.",  # an identifier
        "🌐 **هاست / نشانی پایه:** `{base_url}`",  # translated, and it must stay so
    ):
        assert latin_runs(value) == [], f"false positive on {value!r}"


def test_every_allowance_is_used():
    """A word or name allowed but needed by no value is a leak nobody watches."""
    data = catalog()
    used_terms: set[str] = set()
    used_phrases: set[str] = set()
    for locale in NON_LATIN:
        for value in data[locale].values():
            text = value
            for pattern in _NOT_PROSE:
                text = pattern.sub(" ", text)
            for match in _RUN.finditer(text):
                run = match.group(0).strip()
                words = run.split()
                if len(words) < 2:
                    continue
                used_phrases.add(run.lower())
                used_terms.update(word.lower().strip("-'’") for word in words)
    assert TERMS - used_terms == set(), (
        f"TERMS entries no value needs: {sorted(TERMS - used_terms)}"
    )
    assert PHRASES - used_phrases == set(), (
        f"PHRASES entries no value needs: {sorted(PHRASES - used_phrases)}"
    )


def test_the_scan_knows_which_locales_it_cannot_read():
    """A Latin-script locale must never be added to NON_LATIN and silently pass."""
    data = catalog()
    for locale, values in data.items():
        if locale == "en":
            continue
        share = sum(latin_share(value) for value in values.values()) / len(values)
        if locale in NON_LATIN:
            assert share < 0.35, (
                f"{locale} is in NON_LATIN but {share:.0%} of its letters are "
                "Latin — the scan would flag its own alphabet"
            )
        else:
            assert share > 0.35, (
                f"{locale} is {share:.0%} Latin but is not scanned — either scan "
                "it or say why it cannot be"
            )


# --------------------------------------------------------------------------- #
# the eight terms the scan cannot see, pinned by hand
# --------------------------------------------------------------------------- #
def test_every_retired_term_is_really_an_english_term():
    """Each one is in the English catalog, so the list is a set of real terms."""
    english = catalog()["en"]
    for term in RETIRED_ENGLISH:
        hits = [key for key, value in english.items() if _contains(value, term)]
        assert hits, f"{term!r} is not in the English catalog — drop it from the list"


def test_no_retired_term_is_left_in_any_translated_locale():
    data = catalog()
    offenders = {
        f"{locale}/{key}": term
        for locale, values in data.items()
        if locale != "en"
        for key, value in values.items()
        for term in RETIRED_ENGLISH
        if _contains(value, term)
    }
    assert offenders == {}, (
        f"these values still carry an English term: {sorted(offenders.items())}"
    )


def _contains(value: str, term: str) -> bool:
    """Is `term` in the value as prose — not in code, not as a gloss?"""
    text = value
    for pattern in _NOT_PROSE:
        text = pattern.sub(" ", text)
    return re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE) is not None


def test_the_catalog_is_what_we_think_it_is():
    """Guard the guard: ten locales, same keys, and the scan actually ran."""
    data = catalog()
    assert set(data) == {"en", *NON_LATIN, "pt", "uz", "id", "es", "fr", "it"}
    keys = {locale: set(values) for locale, values in data.items()}
    assert len({frozenset(value) for value in keys.values()}) == 1
    assert len(next(iter(keys.values()))) > 1200
    # the scan is not vacuous: it has something to chew on
    assert json.dumps(data, ensure_ascii=False)
    assert unicodedata.name("پ").startswith("ARABIC")
