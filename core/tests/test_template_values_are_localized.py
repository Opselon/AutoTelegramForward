"""A localized template must not be filled with English.

`self._t("ui_cred_fail", error="vault unavailable")` renders a Persian sentence
around an English body: the sentence is in the catalog, the value that fills its
placeholder is not. The literal guard in `scripts/audit_telegram_i18n.py` reads
the *visible* Telegram call (`reply_text`, `edit_text`, the button rows), so a
value that travels through `_t` as a keyword argument is invisible to it — and
so is the whole class. Batch 1 fixed the ones that were interpolated into the
main menu; this file closes the class.

It found three: `restore_failed(error="attach .atf file")` in bot_manager, and
`ui_cred_fail(error="vault unavailable")` plus `ui_match_logic_alert(nxt="ALL")`
in pro_bot_ui — the last one put the untranslated enum token next to the
translated description, so the alert read "Match logic: ALL (همه شروط)" for a
Persian user.

The scanner is AST-based on purpose: an f-string, a literal, and a catalog value
all look the same to a grep, and only the first two are leaks.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from core.infrastructure.telegram.i18n import I18n

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
#: A run of letters — what prose has and a version number or an enum token does not.
PROSE = re.compile(r"[A-Za-z]{4,}")

TRANSLATE_NAMES = {"_t", "t", "translate"}
#: Keyword names that carry a machine value the user must send back verbatim,
#: or a number: those are not copy and have nothing to translate.
TOKEN_KWARGS = {
    "kind", "mode", "token", "status", "id", "idx", "index", "n", "count",
    "p", "r", "s", "v", "min", "max", "value", "version", "page", "pages",
}


def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def t(key: str, lang: str, **params) -> str:
    text = I18n().t(key, lang=lang, **params)
    assert text and text != key, f"{key} is missing from the {lang} catalog"
    return text


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _prose_value(node: ast.expr) -> str:
    """The English prose a keyword argument would print, or an empty string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
        # A catalog key or a bare token ("BOT", "album") has no spaces.
        return text if PROSE.search(text) and " " in text else ""
    if isinstance(node, ast.JoinedStr):
        static = "".join(
            part.value for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        return static if PROSE.search(static) else ""
    return ""


def literal_values_inside_templates() -> list[tuple[str, int, str, str, str]]:
    """(file, line, catalog key, kwarg, text) for every English body."""
    found = []
    for path in sorted(UI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in TRANSLATE_NAMES:
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            if not isinstance(node.args[0].value, str):
                continue
            key = node.args[0].value
            for kw in node.keywords:
                if kw.arg is None or kw.arg in TOKEN_KWARGS:
                    continue
                text = _prose_value(kw.value)
                if text:
                    found.append((path.name, node.lineno, key, kw.arg, text))
    return found


# --------------------------------------------------------------------------- #
# The class: no English body rides inside a localized template
# --------------------------------------------------------------------------- #


def test_no_english_prose_is_interpolated_into_a_catalog_template():
    found = literal_values_inside_templates()
    detail = "\n".join(
        f"  {name}:{line} _t({key!r}, {arg}={text!r})"
        for name, line, key, arg, text in found
    )
    assert found == [], (
        "these values reach the user in English inside an otherwise localized "
        f"template — give each one a catalog key:\n{detail}"
    )


def test_the_scanner_would_notice_one():
    """A guard that cannot fail is not a guard."""
    tree = ast.parse('self._t("ui_cred_fail", error="vault unavailable")')
    call = tree.body[0].value
    assert _prose_value(call.keywords[0].value) == "vault unavailable"
    # ... while a catalog key, a token and a number are left alone.
    for source in ('self._t("k", n=3)', 'self._t("k", mode="BOT")'):
        node = ast.parse(source).body[0].value
        assert _prose_value(node.keywords[0].value) == ""


# --------------------------------------------------------------------------- #
# The values those templates now take exist in all ten locales
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["restore_attach_hint", "ui_cred_vault_unavailable"])
def test_the_new_values_exist_in_every_locale(key: str):
    cat = catalog()
    for locale in LOCALES:
        value = cat[locale].get(key, "")
        assert value.strip(), f"{key} is missing in {locale}"
        assert value != key, f"{key} is a raw key in {locale}"


@pytest.mark.parametrize("key", ["restore_attach_hint", "ui_cred_vault_unavailable"])
def test_the_new_values_are_not_english_outside_english(key: str):
    cat = catalog()
    assert PERSIAN.search(cat["fa"][key]), f"{key} is not translated for Persian"
    for locale in LOCALES:
        if locale == "en":
            continue
        assert cat[locale][key] != cat["en"][key], f"{key} fell back to English in {locale}"


def test_the_restore_error_reads_as_one_sentence_in_every_locale():
    for locale in LOCALES:
        hint = t("restore_attach_hint", locale)
        text = t("restore_failed", locale, error=hint)
        assert hint in text
        assert "attach .atf file" not in text, f"{locale} still shows the English reason"


def test_the_credential_error_reads_as_one_sentence_in_every_locale():
    for locale in LOCALES:
        reason = t("ui_cred_vault_unavailable", locale)
        text = t("ui_cred_fail", locale, error=reason)
        assert reason in text
        assert "vault unavailable" not in text, f"{locale} still shows the English reason"


# --------------------------------------------------------------------------- #
# The logic alert no longer prints the raw machine token
# --------------------------------------------------------------------------- #


def test_the_logic_alert_carries_only_the_translated_description():
    cat = catalog()
    for locale in LOCALES:
        template = cat[locale]["ui_match_logic_alert"]
        assert "{nxt}" not in template, f"{locale} still interpolates the enum token"
        assert set(re.findall(r"\{(\w+)\}", template)) == {"desc"}, locale


def test_the_logic_alert_never_shows_all_or_any():
    for locale in LOCALES:
        text = t("ui_match_logic_alert", locale, desc=t("ui_logic_all", locale))
        if locale == "en":
            continue
        assert "ALL" not in text and "ANY" not in text, f"{locale}: {text!r}"


def test_the_handler_no_longer_passes_the_enum_token():
    source = (UI_DIR / "pro_bot_ui.py").read_text(encoding="utf-8")
    assert 'ui_match_logic_alert", nxt=' not in source
    assert 'ui_match_logic_alert", desc=' in source
